"""Python-side tool definitions for Pi extension bridge."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

JsonObject: TypeAlias = dict[str, Any]
ContentBlock: TypeAlias = dict[str, Any]
ToolExecutionMode: TypeAlias = Literal["parallel", "sequential"]


@dataclass(frozen=True)
class ToolContext:
    """Execution context passed to Python tool handlers."""

    tool_call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolUpdate:
    """Partial result streamed to Pi's ``onUpdate`` callback."""

    content: list[ContentBlock]
    details: JsonObject = field(default_factory=dict)

    @classmethod
    def text(cls, text: str, *, details: JsonObject | None = None) -> "ToolUpdate":
        return cls(content=[{"type": "text", "text": text}], details=details or {})

    def to_wire(self) -> JsonObject:
        return {"content": self.content, "details": self.details}


@dataclass(frozen=True)
class ToolResult:
    """Final result returned to Pi and then to the LLM."""

    content: list[ContentBlock]
    details: JsonObject = field(default_factory=dict)
    terminate: bool | None = None

    @classmethod
    def text(
        cls,
        text: str,
        *,
        details: JsonObject | None = None,
        terminate: bool | None = None,
    ) -> "ToolResult":
        return cls(
            content=[{"type": "text", "text": text}],
            details=details or {},
            terminate=terminate,
        )

    def to_wire(self) -> JsonObject:
        payload: JsonObject = {"content": self.content, "details": self.details}
        if self.terminate is not None:
            payload["terminate"] = self.terminate
        return payload


class ToolHandler(Protocol):
    def __call__(self, args: JsonObject, ctx: ToolContext) -> Any: ...


@dataclass(frozen=True)
class PythonTool:
    """A Python-authored Pi tool."""

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    label: str | None = None
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)
    execution_mode: ToolExecutionMode | None = None

    def manifest_entry(self) -> JsonObject:
        entry: JsonObject = {
            "name": self.name,
            "label": self.label or self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.prompt_snippet:
            entry["promptSnippet"] = self.prompt_snippet
        if self.prompt_guidelines:
            entry["promptGuidelines"] = list(self.prompt_guidelines)
        if self.execution_mode:
            entry["executionMode"] = self.execution_mode
        return entry


class ToolRegistry:
    """Registry and decorator for Python-authored tools."""

    def __init__(self) -> None:
        self._tools: dict[str, PythonTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: JsonObject,
        label: str | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: ToolExecutionMode | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator registering a Python callable as a Pi tool."""

        def decorator(func: ToolHandler) -> ToolHandler:
            self.add(
                PythonTool(
                    name=name,
                    label=label,
                    description=description,
                    parameters=parameters,
                    handler=func,
                    prompt_snippet=prompt_snippet,
                    prompt_guidelines=prompt_guidelines or [],
                    execution_mode=execution_mode,
                )
            )
            return func

        return decorator

    def add(self, tool: PythonTool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> PythonTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def tools(self) -> list[PythonTool]:
        return list(self._tools.values())

    def manifest(self) -> JsonObject:
        return {"protocolVersion": 1, "tools": [tool.manifest_entry() for tool in self.tools()]}


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def normalize_tool_result(value: Any) -> ToolResult:
    """Normalize common handler return values into :class:`ToolResult`."""

    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult.text(value)
    if isinstance(value, dict):
        if "content" in value:
            return ToolResult(
                content=value["content"],
                details=value.get("details") or {},
                terminate=value.get("terminate"),
            )
        return ToolResult.text(str(value), details={"value": value})
    return ToolResult.text(str(value))


def is_async_generator(value: Any) -> bool:
    return inspect.isasyncgen(value)


def is_sync_generator(value: Any) -> bool:
    return inspect.isgenerator(value)


ToolInvocation: TypeAlias = (
    ToolResult
    | str
    | JsonObject
    | Awaitable[Any]
    | AsyncGenerator[ToolUpdate | ToolResult | str | JsonObject, None]
    | Generator[ToolUpdate | ToolResult | str | JsonObject, None, None]
)
