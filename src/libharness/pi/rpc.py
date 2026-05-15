"""Async subprocess client for ``pi --mode rpc``."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonl import StrictJsonlDecoder, dumps_line

JsonObject = dict[str, Any]
EventHandler = Callable[[JsonObject], None | Awaitable[None]]
UiHandler = Callable[[JsonObject], JsonObject | None | Awaitable[JsonObject | None]]


class PiRpcError(RuntimeError):
    """Raised when Pi returns a failed RPC response."""

    def __init__(self, command: str, error: str, response: JsonObject | None = None) -> None:
        super().__init__(f"Pi RPC command {command!r} failed: {error}")
        self.command = command
        self.error = error
        self.response = response or {}


class PiRpcProcessError(RuntimeError):
    """Raised when the Pi subprocess cannot be started or exits unexpectedly."""


@dataclass(slots=True)
class PiLaunchConfig:
    """Launch settings for a Pi RPC subprocess."""

    pi_command: str | Sequence[str] = "pi"
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    no_session: bool = True
    offline: bool = True
    no_extensions: bool = True
    no_skills: bool = True
    no_prompt_templates: bool = True
    no_context_files: bool = True
    no_builtin_tools: bool = False
    tools: Sequence[str] | None = None
    session_dir: str | Path | None = None
    session: str | None = None
    startup_timeout: float = 5.0
    request_timeout: float = 30.0
    extra_args: Sequence[str] = field(default_factory=tuple)

    def base_argv(self) -> list[str]:
        if isinstance(self.pi_command, str):
            argv = [self.pi_command]
        else:
            argv = list(self.pi_command)
        argv.extend(["--mode", "rpc"])
        if self.provider:
            argv.extend(["--provider", self.provider])
        if self.model:
            argv.extend(["--model", self.model])
        if self.no_session:
            argv.append("--no-session")
        if self.offline:
            argv.append("--offline")
        if self.session_dir:
            argv.extend(["--session-dir", str(self.session_dir)])
        if self.session:
            argv.extend(["--session", self.session])
        if self.no_extensions:
            argv.append("--no-extensions")
        if self.no_skills:
            argv.append("--no-skills")
        if self.no_prompt_templates:
            argv.append("--no-prompt-templates")
        if self.no_context_files:
            argv.append("--no-context-files")
        if self.no_builtin_tools:
            argv.append("--no-builtin-tools")
        if self.tools is not None:
            argv.extend(["--tools", ",".join(self.tools)])
        argv.extend(self.extra_args)
        return argv


class PiRpcClient:
    """Async Python client for Pi's LF-delimited JSON RPC mode."""

    def __init__(self, config: PiLaunchConfig | None = None) -> None:
        self.config = config or PiLaunchConfig()
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}
        self._events: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._event_handlers: list[EventHandler] = []
        self._ui_handlers: dict[str, UiHandler] = {}
        self._fallback_ui_handler: UiHandler | None = None
        self._stderr_chunks: list[str] = []
        self._request_counter = 0
        self._closed = False

    @property
    def stderr(self) -> str:
        return "".join(self._stderr_chunks)

    def argv(self, *, extension_paths: Sequence[str | Path] = ()) -> list[str]:
        argv = self.config.base_argv()
        for extension_path in extension_paths:
            argv.extend(["--extension", str(extension_path)])
        return argv

    async def __aenter__(self) -> PiRpcClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def on_event(self, handler: EventHandler) -> Callable[[], None]:
        self._event_handlers.append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._event_handlers.remove(handler)

        return unsubscribe

    def set_extension_ui_handler(self, method: str | None, handler: UiHandler | None) -> None:
        """Install a handler for extension UI requests.

        ``method=None`` installs a fallback handler. Without a handler, blocking
        dialog methods are safely cancelled in headless RPC runs.
        """

        if method is None:
            self._fallback_ui_handler = handler
        elif handler is None:
            self._ui_handlers.pop(method, None)
        else:
            self._ui_handlers[method] = handler

    async def start(self, *, extension_paths: Sequence[str | Path] = ()) -> None:
        if self.process is not None:
            raise PiRpcProcessError("Pi RPC client is already started")
        argv = self.argv(extension_paths=extension_paths)
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        if self.config.offline:
            env.setdefault("PI_OFFLINE", "1")
        if self.config.env:
            env.update(dict(self.config.env))
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise PiRpcProcessError(f"could not start Pi command {argv[0]!r}") from exc

        self._closed = False
        self._reader_task = asyncio.create_task(self._read_stdout_loop(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr_loop(), name="pi-rpc-stderr")
        await asyncio.sleep(min(0.2, max(0.0, self.config.startup_timeout)))
        if self.process.returncode is not None:
            raise PiRpcProcessError(
                f"Pi exited during startup with code {self.process.returncode}. stderr={self.stderr!r}"
            )

    async def close(self) -> None:
        self._closed = True
        proc = self.process
        if proc is None:
            return
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            try:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()

        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(PiRpcProcessError("Pi RPC process closed"))
        self.process = None

    async def send(self, command: Mapping[str, Any], *, timeout: float | None = None) -> JsonObject:
        proc = self.process
        if proc is None or proc.stdin is None:
            raise PiRpcProcessError("Pi RPC client is not started")
        request = dict(command)
        request_id = str(request.get("id") or self._next_request_id())
        request["id"] = request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._pending[request_id] = future
        try:
            proc.stdin.write(dumps_line(request))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise PiRpcProcessError(f"could not write to Pi stdin. stderr={self.stderr!r}") from exc

        try:
            response = await asyncio.wait_for(future, timeout=timeout or self.config.request_timeout)
        finally:
            self._pending.pop(request_id, None)

        if response.get("success") is False:
            raise PiRpcError(str(request.get("type", "unknown")), str(response.get("error", "")), response)
        return response

    async def prompt(self, message: str, *, streaming_behavior: str | None = None, images: list[JsonObject] | None = None, **extra: Any) -> JsonObject:
        request: JsonObject = {"type": "prompt", "message": message, **extra}
        if streaming_behavior is not None:
            request["streamingBehavior"] = streaming_behavior
        if images is not None:
            request["images"] = images
        return await self.send(request)

    async def prompt_and_wait(self, message: str, *, timeout: float = 120.0, **kwargs: Any) -> list[JsonObject]:
        events: list[JsonObject] = []
        done = asyncio.Event()

        async def collect(event: JsonObject) -> None:
            events.append(event)
            if event.get("type") == "agent_end":
                done.set()

        unsubscribe = self.on_event(collect)
        try:
            await self.prompt(message, **kwargs)
            await asyncio.wait_for(done.wait(), timeout=timeout)
            return events
        finally:
            unsubscribe()

    async def steer(self, message: str, *, images: list[JsonObject] | None = None) -> JsonObject:
        request: JsonObject = {"type": "steer", "message": message}
        if images is not None:
            request["images"] = images
        return await self.send(request)

    async def follow_up(self, message: str, *, images: list[JsonObject] | None = None) -> JsonObject:
        request: JsonObject = {"type": "follow_up", "message": message}
        if images is not None:
            request["images"] = images
        return await self.send(request)

    async def abort(self) -> JsonObject:
        return await self.send({"type": "abort"})

    async def new_session(self) -> JsonObject:
        return await self.send({"type": "new_session"})

    async def get_state(self) -> JsonObject:
        return dict((await self.send({"type": "get_state"})).get("data") or {})

    async def get_messages(self) -> list[JsonObject]:
        data = (await self.send({"type": "get_messages"})).get("data") or {}
        return list(data.get("messages") or [])

    async def get_commands(self) -> list[JsonObject]:
        data = (await self.send({"type": "get_commands"})).get("data") or {}
        return list(data.get("commands") or [])

    async def get_available_models(self) -> list[JsonObject]:
        data = (await self.send({"type": "get_available_models"})).get("data") or {}
        return list(data.get("models") or data.get("availableModels") or [])

    async def set_model(self, provider: str, model: str) -> JsonObject:
        return await self.send({"type": "set_model", "provider": provider, "model": model})

    async def bash(self, command: str) -> JsonObject:
        return dict((await self.send({"type": "bash", "command": command})).get("data") or {})

    async def next_event(self, *, timeout: float | None = None) -> JsonObject:
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout=timeout)

    async def wait_for_event(self, event_type: str, *, timeout: float | None = None) -> JsonObject:
        while True:
            event = await self.next_event(timeout=timeout)
            if event.get("type") == event_type:
                return event

    async def get_last_assistant_text(self) -> str | None:
        for message in reversed(await self.get_messages()):
            if message.get("role") != "assistant":
                continue
            chunks: list[str] = []
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
            if chunks:
                return "\n".join(chunks)
        return None

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"py-{self._request_counter}-{uuid.uuid4().hex}"

    async def _read_stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        decoder = StrictJsonlDecoder()
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                for value in decoder.feed(chunk):
                    if isinstance(value, dict):
                        await self._handle_message(value)
        finally:
            if not self._closed:
                self._fail_pending(PiRpcProcessError(f"Pi stdout closed. stderr={self.stderr!r}"))

    async def _read_stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            self._stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

    async def _handle_message(self, message: JsonObject) -> None:
        if message.get("type") == "response":
            request_id = message.get("id")
            future = self._pending.get(str(request_id)) if request_id is not None else None
            if future and not future.done():
                future.set_result(message)
            return

        if message.get("type") == "extension_ui_request":
            await self._dispatch_event(message)
            await self._handle_extension_ui_request(message)
            return

        await self._dispatch_event(message)

    async def _dispatch_event(self, event: JsonObject) -> None:
        await self._events.put(event)
        for handler in list(self._event_handlers):
            result = handler(event)
            if inspect.isawaitable(result):
                await result

    async def _handle_extension_ui_request(self, request: JsonObject) -> None:
        method = str(request.get("method") or "")
        request_id = request.get("id")
        if not isinstance(request_id, str):
            return
        if method in {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}:
            return

        handler = self._ui_handlers.get(method) or self._fallback_ui_handler
        response: JsonObject | None = None
        if handler is not None:
            maybe = handler(request)
            response = await maybe if inspect.isawaitable(maybe) else maybe
        if response is None:
            response = self._default_ui_response(method)
        await self._send_extension_ui_response({"type": "extension_ui_response", "id": request_id, **response})

    def _default_ui_response(self, method: str) -> JsonObject:
        if method == "confirm":
            return {"confirmed": False}
        if method in {"select", "input", "editor"}:
            return {"cancelled": True}
        return {"cancelled": True}

    async def _send_extension_ui_response(self, response: JsonObject) -> None:
        proc = self.process
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        proc.stdin.write(dumps_line(response))
        await proc.stdin.drain()

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
