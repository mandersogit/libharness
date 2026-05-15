"""Python tool registry and schema inference for the Pi bridge."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import re
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-.]{0,127}$")
_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


class ToolError(RuntimeError):
    """Raised for invalid tool registration or execution."""


@dataclass(slots=True)
class ToolResult:
    """Normalized result shape consumed by the TypeScript bridge.

    Pi's tool result model is content-block oriented. This class preserves that
    shape while providing ergonomic constructors for common text results.
    """

    content: list[dict[str, Any]]
    details: dict[str, Any] = field(default_factory=dict)
    terminate: bool | None = None

    @classmethod
    def text(cls, text: str, *, details: Mapping[str, Any] | None = None, terminate: bool | None = None) -> ToolResult:
        return cls(content=[{"type": "text", "text": text}], details=dict(details or {}), terminate=terminate)

    @classmethod
    def json(cls, value: Any, *, details: Mapping[str, Any] | None = None, terminate: bool | None = None) -> ToolResult:
        return cls.text(json.dumps(value, ensure_ascii=False, indent=2), details=details or {"value": value}, terminate=terminate)

    @classmethod
    def image(cls, url: str, *, details: Mapping[str, Any] | None = None, terminate: bool | None = None) -> ToolResult:
        return cls(content=[{"type": "image", "url": url}], details=dict(details or {}), terminate=terminate)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"content": self.content, "details": self.details}
        if self.terminate is not None:
            out["terminate"] = bool(self.terminate)
        return out


@dataclass(slots=True)
class ToolContext:
    """Execution context passed to Python tools that request it."""

    tool_call_id: str
    tool_name: str
    cwd: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _cancelled: asyncio.Event | None = None
    _update_callback: Callable[[ToolResult | Mapping[str, Any] | str], Any] | None = None

    @property
    def cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled.is_set())

    async def update(
        self,
        value: ToolResult | Mapping[str, Any] | str,
        *,
        details: Mapping[str, Any] | None = None,
        terminate: bool | None = None,
    ) -> None:
        if details is not None or terminate is not None:
            if isinstance(value, ToolResult):
                value.details.update(dict(details or {}))
                if terminate is not None:
                    value.terminate = terminate
            elif isinstance(value, str):
                value = ToolResult.text(value, details=details, terminate=terminate)
            else:
                value = ToolResult.json(value, details=details, terminate=terminate)
        if self._update_callback is None:
            return
        outcome = self._update_callback(value)
        if inspect.isawaitable(outcome):
            await outcome


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_OBJECT_SCHEMA))
    label: str | None = None
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    execution_mode: Literal["sequential", "parallel"] | None = None

    def to_manifest(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label or self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.prompt_snippet:
            out["promptSnippet"] = self.prompt_snippet
        if self.prompt_guidelines:
            out["promptGuidelines"] = list(self.prompt_guidelines)
        if self.execution_mode:
            out["executionMode"] = self.execution_mode
        return out


@dataclass(slots=True)
class RegisteredTool:
    spec: ToolSpec
    func: Callable[..., Any]
    pass_context: bool = False
    context_param: str | None = None

    def call(self, params: Mapping[str, Any], ctx: ToolContext) -> Any:
        kwargs = dict(params or {})
        if self.pass_context:
            if self.context_param:
                kwargs[self.context_param] = ctx
            else:
                kwargs["ctx"] = ctx
        return self.func(**kwargs)


class ToolRegistry:
    """Register Python callables as Pi tools.

    The registry supports both explicit JSON Schema and conservative schema
    inference from Python type annotations. Explicit schema is recommended for
    externally exposed tools; inference is useful for local/internal workflows.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        label: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | tuple[str, ...] | None = None,
        execution_mode: Literal["sequential", "parallel"] | None = None,
        pass_context: bool | None = None,
    ) -> Callable[..., Any]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            _validate_name(tool_name)
            if tool_name in self._tools:
                raise ToolError(f"tool {tool_name!r} already registered")

            doc = inspect.getdoc(fn) or ""
            desc = description or doc.split("\n", 1)[0] or tool_name
            ctx_param, inferred_pass_context = _detect_context_param(fn)
            effective_pass_context = inferred_pass_context if pass_context is None else pass_context
            schema = dict(parameters) if parameters is not None else _schema_from_signature(fn, skip_param=ctx_param if effective_pass_context else None)

            spec = ToolSpec(
                name=tool_name,
                label=label,
                description=desc,
                parameters=schema,
                prompt_snippet=prompt_snippet,
                prompt_guidelines=tuple(prompt_guidelines or ()),
                execution_mode=execution_mode,
            )
            self._tools[tool_name] = RegisteredTool(spec=spec, func=fn, pass_context=effective_pass_context, context_param=ctx_param)
            return fn

        if func is None:
            return decorator
        return decorator(func)

    tool = register

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool {name!r}") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())

    def manifest(self) -> dict[str, Any]:
        return {"tools": [registered.spec.to_manifest() for registered in self._tools.values()]}


def normalize_tool_value(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if value is None:
        return ToolResult.text("")
    if isinstance(value, str):
        return ToolResult.text(value)
    if isinstance(value, (int, float, bool)):
        return ToolResult.text(str(value), details={"value": value})
    if isinstance(value, Mapping):
        mapping = dict(value)
        if isinstance(mapping.get("content"), list):
            return ToolResult(content=list(mapping["content"]), details=dict(mapping.get("details") or {}), terminate=mapping.get("terminate"))
        return ToolResult.json(mapping)
    if not isinstance(value, type) and dataclasses.is_dataclass(value):
        # mypy's TypeGuard on is_dataclass widens to instance-or-type; the
        # isinstance(value, type) guard above already excludes the type case.
        return ToolResult.json(dataclasses.asdict(value))  # type: ignore[arg-type]
    return ToolResult.text(str(value), details={"repr": repr(value)})


async def collect_tool_result(value: Any, ctx: ToolContext) -> ToolResult:
    """Normalize synchronous, async, generator, and async-generator results."""

    if inspect.isawaitable(value):
        value = await value

    final: ToolResult | None = None
    if inspect.isasyncgen(value):
        async for item in value:
            if ctx.cancelled:
                raise asyncio.CancelledError()
            result = normalize_tool_value(item)
            final = result
            await ctx.update(result)
        return final or ToolResult.text("")

    if inspect.isgenerator(value):
        for item in value:
            if ctx.cancelled:
                raise asyncio.CancelledError()
            result = normalize_tool_value(item)
            final = result
            await ctx.update(result)
        return final or ToolResult.text("")

    return normalize_tool_value(value)


def exception_to_wire(exc: BaseException) -> dict[str, Any]:
    return {
        "error": str(exc) or exc.__class__.__name__,
        "details": {
            "exceptionType": exc.__class__.__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        },
    }


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ToolError(
            "tool names must start with a letter or underscore and contain only letters, digits, underscore, hyphen, or dot"
        )


def _detect_context_param(fn: Callable[..., Any]) -> tuple[str | None, bool]:
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = getattr(fn, "__annotations__", {}) or {}
    sig = inspect.signature(fn)
    for param_name, param in sig.parameters.items():
        hint = hints.get(param_name, param.annotation)
        if param_name in {"ctx", "context", "tool_context"}:
            return param_name, True
        if hint is ToolContext:
            return param_name, True
    return None, False


def _schema_from_signature(fn: Callable[..., Any], *, skip_param: str | None) -> dict[str, Any]:
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = getattr(fn, "__annotations__", {}) or {}
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == skip_param:
            continue
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise ToolError(f"tool {fn.__name__!r} parameter {name!r} must be keyword-addressable")
        hint = hints.get(name, param.annotation)
        schema = _schema_for_type(hint if hint is not inspect._empty else Any)
        if param.default is not inspect._empty:
            schema = dict(schema)
            try:
                json.dumps(param.default)
                schema["default"] = param.default
            except TypeError:
                pass
        else:
            required.append(name)
        properties[name] = schema
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _schema_for_type(annotation: Any) -> dict[str, Any]:
    if annotation in (Any, inspect._empty):
        return {}
    if annotation is None or annotation is NoneType:
        return {"type": "null"}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        values = [member.value for member in annotation]
        return {"enum": values}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        return {"enum": list(args)}
    if origin in (Union, UnionType):
        schemas = [_schema_for_type(arg) for arg in args]
        return {"anyOf": schemas}
    if origin in (list, tuple, set, frozenset):
        item_type = args[0] if args else Any
        return {"type": "array", "items": _schema_for_type(item_type)}
    if origin in (dict, Mapping):
        value_type = args[1] if len(args) == 2 else Any
        return {"type": "object", "additionalProperties": _schema_for_type(value_type)}

    if dataclasses.is_dataclass(annotation):
        props: dict[str, Any] = {}
        required: list[str] = []
        for field_info in dataclasses.fields(annotation):
            props[field_info.name] = _schema_for_type(field_info.type)
            if field_info.default is dataclasses.MISSING and field_info.default_factory is dataclasses.MISSING:
                required.append(field_info.name)
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    return {}
