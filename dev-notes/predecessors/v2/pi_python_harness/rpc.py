from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence

JsonObject = dict[str, Any]
EventHandler = Callable[[JsonObject], None | Awaitable[None]]
UiHandler = Callable[[JsonObject], JsonObject | Awaitable[JsonObject | None] | None]


class PiRpcError(RuntimeError):
    """Raised when Pi returns an RPC error response."""

    def __init__(self, command: str, error: str, response: JsonObject | None = None) -> None:
        super().__init__(f"Pi RPC command {command!r} failed: {error}")
        self.command = command
        self.error = error
        self.response = response or {}


class PiRpcProcessError(RuntimeError):
    """Raised when the Pi subprocess cannot be started or exits unexpectedly."""


@dataclass(slots=True)
class PiLaunchConfig:
    """Launch settings for a Pi RPC subprocess.

    `pi_command` may be either a string command (for example, "pi") or a sequence
    (for example, ["node", "/path/to/dist/cli.js"]). `extra_args` are appended
    after `--mode rpc`.
    """

    pi_command: str | Sequence[str] = "pi"
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    extra_args: Sequence[str] = field(default_factory=tuple)
    startup_timeout: float = 5.0
    request_timeout: float = 30.0

    def argv(self) -> list[str]:
        if isinstance(self.pi_command, str):
            argv = [self.pi_command]
        else:
            argv = list(self.pi_command)
        argv.extend(["--mode", "rpc"])
        if self.provider:
            argv.extend(["--provider", self.provider])
        if self.model:
            argv.extend(["--model", self.model])
        argv.extend(self.extra_args)
        return argv


class PiRpcClient:
    """Async Python client for Pi's JSONL RPC mode.

    The client implements strict LF-delimited JSONL framing. It never uses a
    line reader that treats Unicode line separators as record boundaries.
    """

    def __init__(self, config: PiLaunchConfig | None = None) -> None:
        self.config = config or PiLaunchConfig()
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[JsonObject]] = {}
        self._request_counter = 0
        self._event_handlers: list[EventHandler] = []
        self._event_queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._stderr_chunks: list[str] = []
        self._ui_handler: UiHandler | None = None
        self._closed = False

    @property
    def stderr(self) -> str:
        return "".join(self._stderr_chunks)

    async def __aenter__(self) -> "PiRpcClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    def on_event(self, handler: EventHandler) -> Callable[[], None]:
        self._event_handlers.append(handler)

        def unsubscribe() -> None:
            try:
                self._event_handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def set_extension_ui_handler(self, handler: UiHandler | None) -> None:
        """Set a handler for Pi extension UI requests.

        If no handler is installed, dialog-like requests are cancelled. Fire-and-
        forget requests such as notify/setStatus are acknowledged by doing
        nothing, matching Pi's RPC protocol.
        """

        self._ui_handler = handler

    async def start(self) -> None:
        if self.process is not None:
            raise PiRpcProcessError("Pi RPC client is already started")
        argv = self.config.argv()
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_OFFLINE", "1")
        if self.config.env:
            env.update(self.config.env)
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
            raise PiRpcProcessError(f"Could not start Pi command {argv[0]!r}") from exc
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_stdout_loop(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr_loop(), name="pi-rpc-stderr")
        await asyncio.sleep(min(self.config.startup_timeout, 0.2))
        if self.process.returncode is not None:
            raise PiRpcProcessError(
                f"Pi exited during startup with code {self.process.returncode}. stderr={self.stderr!r}"
            )

    async def stop(self) -> None:
        self._closed = True
        proc = self.process
        if proc is None:
            return
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write_eof()
            except (BrokenPipeError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            if sys.platform == "win32":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self.process = None

    async def send(self, command: Mapping[str, Any], timeout: float | None = None) -> JsonObject:
        proc = self.process
        if proc is None or proc.stdin is None:
            raise PiRpcProcessError("Pi RPC client is not started")
        request_id = command.get("id") or self._next_request_id()
        payload = dict(command)
        payload["id"] = request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._pending[str(request_id)] = future
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            proc.stdin.write(wire.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(str(request_id), None)
            raise PiRpcProcessError(f"Could not write to Pi stdin. stderr={self.stderr!r}") from exc
        response = await asyncio.wait_for(future, timeout=timeout or self.config.request_timeout)
        if response.get("success") is False:
            raise PiRpcError(str(response.get("command", command.get("type", "unknown"))), str(response.get("error", "")), response)
        return response

    async def prompt(self, message: str, **kwargs: Any) -> JsonObject:
        return await self.send({"type": "prompt", "message": message, **kwargs})

    async def prompt_and_wait(self, message: str, timeout: float = 120.0, **kwargs: Any) -> list[JsonObject]:
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

    async def get_state(self) -> JsonObject:
        return (await self.send({"type": "get_state"}))["data"]

    async def get_messages(self) -> list[JsonObject]:
        return (await self.send({"type": "get_messages"}))["data"]["messages"]

    async def get_commands(self) -> list[JsonObject]:
        return (await self.send({"type": "get_commands"}))["data"]["commands"]

    async def bash(self, command: str) -> JsonObject:
        return (await self.send({"type": "bash", "command": command}))["data"]

    async def export_html(self, output_path: str | None = None) -> str:
        command: JsonObject = {"type": "export_html"}
        if output_path:
            command["outputPath"] = output_path
        return (await self.send(command))["data"]["path"]

    async def next_event(self, timeout: float | None = None) -> JsonObject:
        if timeout is None:
            return await self._event_queue.get()
        return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"py_{self._request_counter}"

    async def _read_stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                break
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("type") == "response" and data.get("id") in self._pending:
                future = self._pending.pop(str(data.get("id")))
                if not future.done():
                    future.set_result(data)
                continue
            if isinstance(data, dict) and data.get("type") == "extension_ui_request":
                await self._handle_extension_ui_request(data)
                await self._dispatch_event(data)
                continue
            if isinstance(data, dict):
                await self._dispatch_event(data)
        if not self._closed:
            for request_id, future in list(self._pending.items()):
                if not future.done():
                    future.set_exception(PiRpcProcessError(f"Pi stdout closed. stderr={self.stderr!r}"))
                self._pending.pop(request_id, None)

    async def _read_stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            self._stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

    async def _dispatch_event(self, event: JsonObject) -> None:
        await self._event_queue.put(event)
        for handler in list(self._event_handlers):
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result

    async def _handle_extension_ui_request(self, request: JsonObject) -> None:
        method = request.get("method")
        request_id = request.get("id")
        if not isinstance(request_id, str):
            return
        if method in {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}:
            return
        response: JsonObject | None = None
        if self._ui_handler is not None:
            maybe = self._ui_handler(request)
            response = await maybe if asyncio.iscoroutine(maybe) else maybe
        if response is None:
            response = {"type": "extension_ui_response", "id": request_id, "cancelled": True}
        else:
            response = {"type": "extension_ui_response", "id": request_id, **response}
        proc = self.process
        if proc and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
            await proc.stdin.drain()
