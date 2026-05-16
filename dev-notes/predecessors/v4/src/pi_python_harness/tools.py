from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from types import NoneType
from typing import Any, Callable, Mapping, Optional, Union, get_args, get_origin, get_type_hints

from .types import ToolCallContext, ToolResult


class ToolError(Exception):
    pass


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty or annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, getattr(__import__("types"), "UnionType", object)):
        non_none = [arg for arg in args if arg is not NoneType]
        if len(non_none) == 1 and len(args) == 2:
            schema = _annotation_to_schema(non_none[0])
            schema = dict(schema)
            schema["nullable"] = True
            return schema
        return {"anyOf": [_annotation_to_schema(arg) for arg in non_none]}

    if origin in (list, tuple, set, frozenset):
        item_schema = _annotation_to_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

    if origin is dict:
        value_schema = _annotation_to_schema(args[1]) if len(args) >= 2 else {}
        return {"type": "object", "additionalProperties": value_schema}

    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is dict:
        return {"type": "object"}
    if annotation is list:
        return {"type": "array"}
    if annotation is None or annotation is NoneType:
        return {"type": "null"}
    return {}


def infer_json_schema(func: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name in {"ctx", "context", "tool_context"}:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        schema = _annotation_to_schema(annotation)
        if param.default is not inspect.Signature.empty:
            try:
                json.dumps(param.default)
                schema = {**schema, "default": param.default}
            except TypeError:
                pass
        else:
            required.append(name)
        properties[name] = schema

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def normalize_tool_result(value: Any) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult.text(value)
    if isinstance(value, Mapping):
        if "content" in value:
            payload = dict(value)
            return ToolResult(
                content=list(payload.get("content", [])),
                details=payload.get("details", {}),
                terminate=payload.get("terminate"),
            )
        return ToolResult.text(json.dumps(value, ensure_ascii=False, indent=2), details=dict(value))
    if value is None:
        return ToolResult.text("", details={})
    return ToolResult.text(str(value), details=value if isinstance(value, (int, float, bool, list, dict)) else {})


@dataclass
class PythonTool:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)
    execution_mode: str | None = None

    def manifest_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.prompt_snippet:
            entry["promptSnippet"] = self.prompt_snippet
        if self.prompt_guidelines:
            entry["promptGuidelines"] = self.prompt_guidelines
        if self.execution_mode:
            entry["executionMode"] = self.execution_mode
        return entry

    async def invoke(self, params: Mapping[str, Any], ctx: ToolCallContext) -> ToolResult:
        kwargs = dict(params)
        sig = inspect.signature(self.func)
        for context_name in ("ctx", "context", "tool_context"):
            if context_name in sig.parameters:
                kwargs[context_name] = ctx
                break

        result = self.func(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return normalize_tool_result(result)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, PythonTool] = {}

    @property
    def tools(self) -> Mapping[str, PythonTool]:
        return self._tools

    def register(self, tool: PythonTool) -> PythonTool:
        if tool.name in self._tools:
            raise ToolError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        label: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: str | None = None,
    ) -> Callable[[Callable[..., Any]], PythonTool] | PythonTool:
        def decorator(f: Callable[..., Any]) -> PythonTool:
            tool_name = name or f.__name__
            doc = inspect.getdoc(f) or ""
            py_tool = PythonTool(
                name=tool_name,
                label=label or tool_name.replace("_", " ").title(),
                description=description or doc or tool_name,
                parameters=parameters or infer_json_schema(f),
                func=f,
                prompt_snippet=prompt_snippet,
                prompt_guidelines=prompt_guidelines or [],
                execution_mode=execution_mode,
            )
            return self.register(py_tool)

        if func is not None:
            return decorator(func)
        return decorator

    def manifest(self) -> dict[str, Any]:
        return {"version": 1, "tools": [tool.manifest_entry() for tool in self._tools.values()]}

    async def invoke(self, tool_name: str, params: Mapping[str, Any], ctx: ToolCallContext) -> ToolResult:
        try:
            tool = self._tools[tool_name]
        except KeyError as exc:
            raise ToolError(f"Unknown tool: {tool_name}") from exc
        return await tool.invoke(params, ctx)
