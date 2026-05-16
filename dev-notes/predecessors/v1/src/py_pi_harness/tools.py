from __future__ import annotations

import asyncio
import inspect
import json
import typing as t
from dataclasses import dataclass, field
from types import NoneType, UnionType

JsonObject = dict[str, t.Any]
JsonSchema = dict[str, t.Any]
ContentBlock = dict[str, t.Any]


@dataclass(frozen=True)
class ToolResult:
    """Result shape expected by Pi's AgentToolResult.

    Pi represents errors by exceptions. `ToolResult` therefore has no `is_error`
    field; raise from the Python tool to emit an error tool result in Pi.
    """

    content: list[ContentBlock]
    details: t.Any = field(default_factory=dict)
    terminate: bool | None = None

    @staticmethod
    def text(text: str, *, details: t.Any | None = None, terminate: bool | None = None) -> "ToolResult":
        return ToolResult(content=[{"type": "text", "text": text}], details={} if details is None else details, terminate=terminate)

    @staticmethod
    def image(data: str, mime_type: str, *, details: t.Any | None = None) -> "ToolResult":
        return ToolResult(content=[{"type": "image", "data": data, "mimeType": mime_type}], details={} if details is None else details)

    def to_frame(self) -> JsonObject:
        data: JsonObject = {"content": self.content, "details": self.details}
        if self.terminate is not None:
            data["terminate"] = self.terminate
        return data


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonSchema
    label: str | None = None
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)
    execution_mode: t.Literal["sequential", "parallel"] | None = None

    def to_manifest(self) -> JsonObject:
        out: JsonObject = {
            "name": self.name,
            "label": self.label or self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.prompt_snippet:
            out["promptSnippet"] = self.prompt_snippet
        if self.prompt_guidelines:
            out["promptGuidelines"] = self.prompt_guidelines
        if self.execution_mode:
            out["executionMode"] = self.execution_mode
        return out


class ToolContext:
    """Context object injected into Python tools when requested.

    A tool can call `ctx.update(...)` to stream partial results to Pi. The partial
    result uses the same AgentToolResult shape as the final result.
    """

    def __init__(self, tool_call_id: str, update_callback: t.Callable[[ToolResult], t.Awaitable[None]] | None = None) -> None:
        self.tool_call_id = tool_call_id
        self._update_callback = update_callback

    async def update(self, value: t.Any, *, details: t.Any | None = None, terminate: bool | None = None) -> None:
        if self._update_callback is None:
            return
        await self._update_callback(normalize_tool_result(value, details=details, terminate=terminate))


def normalize_tool_result(value: t.Any, *, details: t.Any | None = None, terminate: bool | None = None) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult.text(value, details=details, terminate=terminate)
    if isinstance(value, list) and all(isinstance(item, dict) and "type" in item for item in value):
        return ToolResult(content=t.cast(list[ContentBlock], value), details={} if details is None else details, terminate=terminate)
    return ToolResult.text(json.dumps(value, indent=2, sort_keys=True), details=value if details is None else details, terminate=terminate)


@dataclass
class RegisteredPythonTool:
    spec: ToolSpec
    func: t.Callable[..., t.Any]
    pass_context: bool = False

    async def execute(self, params: JsonObject, ctx: ToolContext) -> ToolResult:
        kwargs = dict(params)
        if self.pass_context:
            kwargs["ctx"] = ctx

        result = self.func(**kwargs)
        if inspect.isawaitable(result):
            result = await t.cast(t.Awaitable[t.Any], result)
        return normalize_tool_result(result)


class ToolRegistry:
    """Registry for Python-authored Pi tools."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredPythonTool] = {}

    def register(
        self,
        func: t.Callable[..., t.Any],
        *,
        name: str | None = None,
        description: str | None = None,
        label: str | None = None,
        parameters: JsonSchema | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: t.Literal["sequential", "parallel"] | None = None,
        pass_context: bool | None = None,
    ) -> t.Callable[..., t.Any]:
        tool_name = name or func.__name__
        if not tool_name:
            raise ValueError("Tool name must be non-empty")
        if tool_name in self._tools:
            raise ValueError(f"Tool already registered: {tool_name}")

        signature = inspect.signature(func)
        inferred_pass_context = _signature_wants_context(signature)
        schema = parameters or _schema_from_signature(signature)
        spec = ToolSpec(
            name=tool_name,
            label=label or tool_name,
            description=description or inspect.getdoc(func) or tool_name,
            parameters=schema,
            prompt_snippet=prompt_snippet,
            prompt_guidelines=prompt_guidelines or [],
            execution_mode=execution_mode,
        )
        self._tools[tool_name] = RegisteredPythonTool(
            spec=spec,
            func=func,
            pass_context=inferred_pass_context if pass_context is None else pass_context,
        )
        return func

    def tool(self, **kwargs: t.Any) -> t.Callable[[t.Callable[..., t.Any]], t.Callable[..., t.Any]]:
        """Decorator for registering a Python function as a Pi tool."""

        def decorator(func: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
            return self.register(func, **kwargs)

        return decorator

    def specs(self) -> list[ToolSpec]:
        return [entry.spec for entry in self._tools.values()]

    def manifest(self) -> list[JsonObject]:
        return [spec.to_manifest() for spec in self.specs()]

    async def execute(self, name: str, params: JsonObject, ctx: ToolContext) -> ToolResult:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown Python tool: {name}") from exc
        return await tool.execute(params, ctx)


def _signature_wants_context(signature: inspect.Signature) -> bool:
    for param in signature.parameters.values():
        if param.name in {"ctx", "context"}:
            return True
        if param.annotation is ToolContext:
            return True
    return False


def _schema_from_signature(signature: inspect.Signature) -> JsonSchema:
    properties: JsonObject = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        if name in {"ctx", "context"} or param.annotation is ToolContext:
            continue
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise TypeError(f"Unsupported tool parameter kind for {name}: {param.kind}")
        annotation = param.annotation if param.annotation is not inspect._empty else str
        schema = _schema_for_type(annotation)
        if param.default is not inspect._empty:
            schema = {**schema, "default": param.default}
        else:
            required.append(name)
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema_for_type(annotation: t.Any) -> JsonSchema:
    origin = t.get_origin(annotation)
    args = t.get_args(annotation)

    if annotation in (str, inspect._empty):
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation in (dict, JsonObject) or origin is dict:
        return {"type": "object"}
    if annotation is list or origin is list:
        item_schema = _schema_for_type(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin in (t.Union, UnionType):
        non_none = [arg for arg in args if arg is not NoneType]
        if len(non_none) == 1 and len(non_none) != len(args):
            return {**_schema_for_type(non_none[0]), "nullable": True}
        return {"anyOf": [_schema_for_type(arg) for arg in non_none]}
    if origin is t.Literal:
        values = list(args)
        schema: JsonSchema = {"enum": values}
        if values:
            schema["type"] = _json_type_for_literal(values[0])
        return schema
    return {"type": "string", "description": f"Serialized value for Python type {annotation!r}"}


def _json_type_for_literal(value: t.Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


async def maybe_await(value: t.Any) -> t.Any:
    if asyncio.iscoroutine(value) or inspect.isawaitable(value):
        return await value
    return value
