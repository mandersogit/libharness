from __future__ import annotations

import dataclasses
import inspect
import json
import sys
import traceback
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, get_args, get_origin, get_type_hints

JsonObject = dict[str, Any]
ToolFunction = Callable[..., Any]


@dataclass(slots=True)
class ToolContext:
    tool_call_id: str | None = None
    signal: Any | None = None
    cwd: str | None = None


@dataclass(slots=True)
class ToolResult:
    content: list[JsonObject]
    details: JsonObject = field(default_factory=dict)
    terminate: bool | None = None

    @classmethod
    def text(cls, text: str, details: Mapping[str, Any] | None = None, terminate: bool | None = None) -> "ToolResult":
        return cls(content=[{"type": "text", "text": text}], details=dict(details or {}), terminate=terminate)

    def to_wire(self) -> JsonObject:
        payload: JsonObject = {"content": self.content, "details": self.details}
        if self.terminate is not None:
            payload["terminate"] = self.terminate
        return payload


@dataclass(slots=True)
class RegisteredPythonTool:
    name: str
    label: str
    description: str
    parameters: JsonObject
    function: ToolFunction
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)
    execution_mode: str | None = None
    pass_context: bool = False

    def to_manifest(self) -> JsonObject:
        payload: JsonObject = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.prompt_snippet:
            payload["promptSnippet"] = self.prompt_snippet
        if self.prompt_guidelines:
            payload["promptGuidelines"] = self.prompt_guidelines
        if self.execution_mode:
            payload["executionMode"] = self.execution_mode
        return payload


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredPythonTool] = {}

    def register(
        self,
        func: ToolFunction | None = None,
        *,
        name: str | None = None,
        label: str | None = None,
        description: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: str | None = None,
        pass_context: bool = False,
    ) -> Callable[[ToolFunction], ToolFunction] | ToolFunction:
        def decorator(fn: ToolFunction) -> ToolFunction:
            tool_name = name or fn.__name__
            desc = description or inspect.getdoc(fn) or f"Python tool {tool_name}"
            schema = dict(parameters) if parameters is not None else schema_from_callable(fn, pass_context=pass_context)
            self._tools[tool_name] = RegisteredPythonTool(
                name=tool_name,
                label=label or tool_name.replace("_", " ").title(),
                description=desc,
                parameters=schema,
                function=fn,
                prompt_snippet=prompt_snippet,
                prompt_guidelines=prompt_guidelines or [],
                execution_mode=execution_mode,
                pass_context=pass_context,
            )
            return fn

        if func is not None:
            return decorator(func)
        return decorator

    def list_tools(self) -> list[JsonObject]:
        return [tool.to_manifest() for tool in self._tools.values()]

    def get(self, name: str) -> RegisteredPythonTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown Python tool: {name}") from exc

    def call(self, name: str, params: Mapping[str, Any], tool_call_id: str | None = None, cwd: str | None = None) -> JsonObject:
        registered = self.get(name)
        kwargs = dict(params)
        if registered.pass_context:
            kwargs["ctx"] = ToolContext(tool_call_id=tool_call_id, cwd=cwd)
        result = registered.function(**kwargs)
        return normalize_tool_result(result)


def tool(*args: Any, **kwargs: Any) -> Any:
    return default_registry.register(*args, **kwargs)


def normalize_tool_result(result: Any) -> JsonObject:
    if isinstance(result, ToolResult):
        return result.to_wire()
    if dataclasses.is_dataclass(result):
        result = dataclasses.asdict(result)
    if isinstance(result, Mapping):
        if "content" in result:
            payload = dict(result)
            payload.setdefault("details", {})
            return payload
        return ToolResult.text(json.dumps(result, ensure_ascii=False), details={"python_type": "mapping"}).to_wire()
    if result is None:
        return ToolResult.text("", details={"python_type": "none"}).to_wire()
    return ToolResult.text(str(result), details={"python_type": type(result).__name__}).to_wire()


def schema_from_callable(fn: ToolFunction, *, pass_context: bool = False) -> JsonObject:
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    properties: JsonObject = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if pass_context and name == "ctx":
            continue
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            continue
        annotation = hints.get(name, param.annotation)
        prop_schema = schema_from_type(annotation)
        if param.default is not inspect._empty:
            prop_schema["default"] = param.default
        else:
            required.append(name)
        properties[name] = prop_schema
    schema: JsonObject = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def schema_from_type(annotation: Any) -> JsonObject:
    if annotation is inspect._empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and len(args) == 2:
            schema = schema_from_type(non_none[0])
            t = schema.get("type")
            if isinstance(t, str):
                schema["type"] = [t, "null"]
            return schema
        return {"anyOf": [schema_from_type(arg) for arg in args]}

    if origin in (list, tuple, set):
        item_type = args[0] if args else Any
        return {"type": "array", "items": schema_from_type(item_type)}
    if origin is dict:
        return {"type": "object"}
    if origin is None:
        if annotation is str:
            return {"type": "string"}
        if annotation is int:
            return {"type": "integer"}
        if annotation is float:
            return {"type": "number"}
        if annotation is bool:
            return {"type": "boolean"}
    return {}


def serve_jsonl(registry: ToolRegistry = None, *, stdin: Any = None, stdout: Any = None) -> None:
    registry = registry or default_registry
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        line = raw[:-1] if raw.endswith("\n") else raw
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(registry, request)
        except Exception as exc:  # keep the bridge alive for subsequent calls
            response = {
                "id": request.get("id") if isinstance(locals().get("request"), dict) else None,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()


def handle_request(registry: ToolRegistry, request: Mapping[str, Any]) -> JsonObject:
    request_id = request.get("id")
    method = request.get("method")
    if method == "list_tools":
        return {"id": request_id, "ok": True, "tools": registry.list_tools()}
    if method == "call_tool":
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            raise TypeError("params must be an object")
        result = registry.call(str(request.get("name")), params, tool_call_id=request.get("toolCallId"), cwd=request.get("cwd"))
        return {"id": request_id, "ok": True, "result": result}
    raise ValueError(f"Unknown method: {method}")


default_registry = ToolRegistry()
