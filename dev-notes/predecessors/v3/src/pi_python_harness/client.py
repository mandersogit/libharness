"""Async Python client for ``pi --mode rpc``."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonl import StrictJsonlBuffer, dumps_line

RpcObject = dict[str, Any]
EventHandler = Callable[[RpcObject], None | Awaitable[None]]
UiHandler = Callable[[RpcObject], RpcObject | None | Awaitable[RpcObject | None]]


class RpcError(RuntimeError):
    """Raised when Pi returns ``success: false`` for an RPC command."""

    def __init__(self, response: RpcObject):
        self.response = response
        super().__init__(str(response.get("error") or response))


class RpcProcessError(RuntimeError):
    """Raised when the Pi subprocess cannot be started or exits unexpectedly."""


@dataclass
class PiRpcClient:
    """Subprocess client for Pi RPC mode.

    ``command`` should normally be ``("pi",)``. For source checkouts you can pass
    a Node invocation such as ``("node", "/path/to/dist/cli.js")``.
    """

    command: Sequence[str] = ("pi",)
    cwd: str | Path | None = None
    env: dict[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    no_session: bool = False
    session_dir: str | Path | None = None
    extra_args: list[str] = field(default_factory=list)
    request_timeout: float = 30.0

    _process: asyncio.subprocess.Process | None = field(default=None, init=False)
    _pending: dict[str, asyncio.Future[RpcObject]] = field(default_factory=dict, init=False)
    _event_handlers: list[EventHandler] = field(default_factory=list, init=False)
    _ui_handlers: dict[str, UiHandler] = field(default_factory=dict, init=False)
    _events: asyncio.Queue[RpcObject] = field(default_factory=asyncio.Queue, init=False)
    _stderr: bytearray = field(default_factory=bytearray, init=False)
    _stdout_task: asyncio.Task[None] | None = field(default=None, init=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False)
    _request_seq: int = field(default=0, init=False)

    @property
    def process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RpcProcessError("Pi RPC process is not started")
        return self._process

    @property
    def stderr_text(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")

    def add_event_handler(self, handler: EventHandler) -> Callable[[], None]:
        self._event_handlers.append(handler)

        def remove() -> None:
            try:
                self._event_handlers.remove(handler)
            except ValueError:
                pass

        return remove

    def set_ui_handler(self, method: str, handler: UiHandler) -> None:
        self._ui_handlers[method] = handler

    def build_args(self) -> list[str]:
        args = [*self.command, "--mode", "rpc"]
        if self.provider:
            args.extend(["--provider", self.provider])
        if self.model:
            args.extend(["--model", self.model])
        if self.no_session:
            args.append("--no-session")
        if self.session_dir:
            args.extend(["--session-dir", str(self.session_dir)])
        args.extend(self.extra_args)
        return args

    async def start(self) -> None:
        if self._process is not None:
            raise RpcProcessError("Pi RPC process is already started")

        env = os.environ.copy()
        if self.env:
            env.update(self.env)

        args = self.build_args()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RpcProcessError(f"failed to start Pi RPC command: {args[0]!r}") from exc

        self._stdout_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="pi-rpc-stderr")

        await asyncio.sleep(0.05)
        if self.process.returncode is not None:
            raise RpcProcessError(
                f"Pi RPC process exited immediately with code {self.process.returncode}: {self.stderr_text}"
            )

    async def close(self) -> None:
        if self._process is None:
            return

        proc = self._process
        self._process = None

        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
            await proc.stdin.wait_closed()

        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

        for task in (self._stdout_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._stdout_task = None
        self._stderr_task = None

        for future in self._pending.values():
            if not future.done():
                future.set_exception(RpcProcessError("Pi RPC process closed"))
        self._pending.clear()

    async def __aenter__(self) -> "PiRpcClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def send(self, command: RpcObject, *, timeout: float | None = None) -> RpcObject:
        if self._process is None or self.process.stdin is None:
            raise RpcProcessError("Pi RPC process is not started")

        request = dict(command)
        request_id = str(request.get("id") or self._next_request_id())
        request["id"] = request_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[RpcObject] = loop.create_future()
        self._pending[request_id] = future

        self.process.stdin.write(dumps_line(request))
        await self.process.stdin.drain()

        try:
            response = await asyncio.wait_for(future, timeout=timeout or self.request_timeout)
        finally:
            self._pending.pop(request_id, None)

        if not response.get("success", False):
            raise RpcError(response)
        return response

    async def prompt(self, message: str, *, streaming_behavior: str | None = None, images: list[RpcObject] | None = None) -> None:
        request: RpcObject = {"type": "prompt", "message": message}
        if streaming_behavior is not None:
            request["streamingBehavior"] = streaming_behavior
        if images is not None:
            request["images"] = images
        await self.send(request)

    async def steer(self, message: str, *, images: list[RpcObject] | None = None) -> None:
        request: RpcObject = {"type": "steer", "message": message}
        if images is not None:
            request["images"] = images
        await self.send(request)

    async def follow_up(self, message: str, *, images: list[RpcObject] | None = None) -> None:
        request: RpcObject = {"type": "follow_up", "message": message}
        if images is not None:
            request["images"] = images
        await self.send(request)

    async def get_state(self) -> RpcObject:
        response = await self.send({"type": "get_state"})
        return dict(response.get("data") or {})

    async def get_messages(self) -> list[RpcObject]:
        response = await self.send({"type": "get_messages"})
        data = response.get("data") or {}
        return list(data.get("messages") or [])

    async def wait_for_event(self, event_type: str, *, timeout: float | None = None) -> RpcObject:
        while True:
            event = await asyncio.wait_for(self._events.get(), timeout=timeout)
            if event.get("type") == event_type:
                return event

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        parser = StrictJsonlBuffer()
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            for obj in parser.feed(chunk):
                if isinstance(obj, dict):
                    await self._handle_message(obj)

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            self._stderr.extend(chunk)

    async def _handle_message(self, message: RpcObject) -> None:
        if message.get("type") == "response":
            request_id = message.get("id")
            future = self._pending.get(str(request_id)) if request_id is not None else None
            if future and not future.done():
                future.set_result(message)
            return

        if message.get("type") == "extension_ui_request":
            await self._handle_ui_request(message)
            return

        await self._events.put(message)
        for handler in list(self._event_handlers):
            result = handler(message)
            if inspect.isawaitable(result):
                await result

    async def _handle_ui_request(self, request: RpcObject) -> None:
        method = str(request.get("method") or "")
        handler = self._ui_handlers.get(method)
        response: RpcObject | None = None
        if handler is not None:
            maybe = handler(request)
            response = await maybe if inspect.isawaitable(maybe) else maybe
        else:
            response = self._default_ui_response(request)

        if response is not None:
            await self._send_ui_response(response)

    def _default_ui_response(self, request: RpcObject) -> RpcObject | None:
        method = request.get("method")
        request_id = request.get("id")
        if method in {"select", "input", "editor"}:
            return {"type": "extension_ui_response", "id": request_id, "cancelled": True}
        if method == "confirm":
            return {"type": "extension_ui_response", "id": request_id, "confirmed": False}
        # notify, setStatus, setWidget, setTitle, set_editor_text are fire-and-forget.
        return None

    async def _send_ui_response(self, response: RpcObject) -> None:
        if self._process is None or self.process.stdin is None:
            return
        self.process.stdin.write(dumps_line(response))
        await self.process.stdin.drain()

    def _next_request_id(self) -> str:
        self._request_seq += 1
        return f"py-{self._request_seq}-{uuid.uuid4().hex}"
