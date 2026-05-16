from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

Json = dict[str, Any] | list[Any] | str | int | float | bool | None


@dataclass(frozen=True)
class TextContent:
    text: str
    type: str = "text"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True)
class ImageContent:
    data: str
    mime_type: str
    type: str = "image"

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "mimeType": self.mime_type}


@dataclass
class ToolResult:
    """Result shape expected by Pi AgentToolResult.

    `content` is sent back to the model. `details` is retained for logs, UI, and
    state reconstruction. `terminate=True` requests early termination when every
    tool in the current batch also terminates.
    """

    content: list[dict[str, Any]]
    details: Any = field(default_factory=dict)
    terminate: bool | None = None

    @classmethod
    def text(cls, text: str, *, details: Any | None = None, terminate: bool | None = None) -> "ToolResult":
        return cls(content=[{"type": "text", "text": text}], details={} if details is None else details, terminate=terminate)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": self.content, "details": self.details}
        if self.terminate is not None:
            payload["terminate"] = self.terminate
        return payload


@dataclass(frozen=True)
class ToolCallContext:
    """Context passed from the TS shim/Pi to a Python tool."""

    tool_call_id: str
    tool_name: str
    cwd: str | None = None
    pi_context: Mapping[str, Any] = field(default_factory=dict)
    update: Callable[[ToolResult | str | Mapping[str, Any]], None] | None = None

    def emit_update(self, result: ToolResult | str | Mapping[str, Any]) -> None:
        if self.update is not None:
            self.update(result)


@dataclass(frozen=True)
class PiLaunchOptions:
    """Launch settings for `pi --mode rpc`."""

    command: Sequence[str] = ("pi",)
    cwd: str | None = None
    provider: str | None = None
    model: str | None = None
    no_session: bool = True
    no_extensions: bool = True
    no_builtin_tools: bool = False
    tools: Sequence[str] | None = None
    session_dir: str | None = None
    session: str | None = None
    extra_args: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def to_argv(self, *, extension_path: str | None = None) -> list[str]:
        argv = [*self.command, "--mode", "rpc"]
        if self.provider:
            argv += ["--provider", self.provider]
        if self.model:
            argv += ["--model", self.model]
        if self.no_session:
            argv.append("--no-session")
        if self.no_extensions:
            argv.append("--no-extensions")
        if self.no_builtin_tools:
            argv.append("--no-builtin-tools")
        if self.tools:
            argv += ["--tools", ",".join(self.tools)]
        if self.session_dir:
            argv += ["--session-dir", self.session_dir]
        if self.session:
            argv += ["--session", self.session]
        if extension_path:
            argv += ["--extension", extension_path]
        argv.extend(self.extra_args)
        return argv
