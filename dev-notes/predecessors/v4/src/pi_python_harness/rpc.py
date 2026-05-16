from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .jsonl import JsonlDecoder, dumps_line, loads_lines
from .types import PiLaunchOptions

EventHandler = Callable[[dict[str, Any]], None | Awaitable[None]]


class PiRpcError(Exception):
    pass


class PiProcessError(PiRpcError):
    pass


@dataclass(frozen=True)
class PendingRequest:
    command: str
    future: asyncio.Future[dict[str, Any]]


class PiRpcClient:
    """Async client for Pi's `--mode rpc` JSONL protocol."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 5.0,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env or {})
        self.startup_timeout = startup_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._request_id = 0
        self._pending: dict[str, PendingRequest] = {}
        self._event_handlers: list[EventHandler] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @classmethod
    def from_launch_options(cls, options: PiLaunchOptions, *, extension_path: str | None = None, env: Mapping[str, str] | None = None) -> "PiRpcClient":
        merged_env = {**dict(options.env), **dict(env or {})}
        return cls(options.to_argv(extension_path=extension_path), cwd=options.cwd, env=merged_env)

    @property
    def stderr_text(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")

    def on_event(self, handler: EventHandler) -> Callable[[], None]:
        self._event_handlers.append(handler)

        def unsubscribe() -> None:
            try:
                self._event_handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    async def start(self) -> None:
        if self.process is not None:
            raise PiRpcError("Pi RPC client is already started")
        env = os.environ.copy()
        env.update(self.env)
        self.process = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="pi-rpc-stderr")
        try:
            await asyncio.wait_for(self._ensure_alive(), timeout=self.startup_timeout)
        except asyncio.TimeoutError as exc:
            raise PiProcessError(f"Pi did not remain healthy during startup. stderr={self.stderr_text!r}") from exc

    async def _ensure_alive(self) -> None:
        assert self.process is not None
        # Give the process one event loop tick plus a short delay to fail fast if
        # the binary/path/env is invalid. A real Pi RPC process does not emit a
        # ready event, so liveness is the only portable startup check.
        await asyncio.sleep(0.1)
        if self.process.returncode is not None:
            raise PiProcessError(f"Pi exited during startup with code {self.process.returncode}. stderr={self.stderr_text!r}")

    async def stop(self, *, kill_timeout: float = 2.0) -> None:
        proc = self.process
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        self.process = None
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(PiProcessError("Pi process stopped before response was received"))
        self._pending.clear()

    async def __aenter__(self) -> "PiRpcClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        decoder = JsonlDecoder()
        try:
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                for event in loads_lines(decoder.feed(chunk)):
                    await self._handle_record(event)
            for event in loads_lines(decoder.flush()):
                await self._handle_record(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(exc)
            self._pending.clear()
        finally:
            proc = self.process
            if proc is not None and proc.returncode is None:
                await proc.wait()
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(PiProcessError(f"Pi stdout ended before response. stderr={self.stderr_text!r}"))
            self._pending.clear()

    async def _handle_record(self, record: Any) -> None:
        if not isinstance(record, dict):
            return
        if record.get("type") == "response" and isinstance(record.get("id"), str):
            request_id = record["id"]
            pending = self._pending.pop(request_id, None)
            if pending is not None and not pending.future.done():
                pending.future.set_result(record)
                return
        await self.events.put(record)
        for handler in list(self._event_handlers):
            result = handler(record)
            if asyncio.iscoroutine(result):
                await result

    async def send(self, command: Mapping[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise PiRpcError("Pi RPC client is not started")
        self._request_id += 1
        request_id = f"py_{self._request_id}"
        full_command = dict(command)
        full_command["id"] = request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = PendingRequest(str(command.get("type", "unknown")), future)
        self.process.stdin.write(dumps_line(full_command))
        await self.process.stdin.drain()
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending.pop(request_id, None)
            raise
        if not response.get("success", False):
            raise PiRpcError(str(response.get("error", f"RPC command failed: {command.get('type')}")))
        return response

    async def notify_extension_ui(self, request_id: str, *, value: str | None = None, confirmed: bool | None = None, cancelled: bool = False) -> None:
        if self.process is None or self.process.stdin is None:
            raise PiRpcError("Pi RPC client is not started")
        if cancelled:
            payload: dict[str, Any] = {"type": "extension_ui_response", "id": request_id, "cancelled": True}
        elif confirmed is not None:
            payload = {"type": "extension_ui_response", "id": request_id, "confirmed": confirmed}
        else:
            payload = {"type": "extension_ui_response", "id": request_id, "value": "" if value is None else value}
        self.process.stdin.write(dumps_line(payload))
        await self.process.stdin.drain()

    async def prompt(self, message: str, **kwargs: Any) -> dict[str, Any]:
        return await self.send({"type": "prompt", "message": message, **kwargs})

    async def get_state(self) -> dict[str, Any]:
        response = await self.send({"type": "get_state"})
        return response.get("data", {})

    async def get_messages(self) -> list[dict[str, Any]]:
        response = await self.send({"type": "get_messages"})
        data = response.get("data", {})
        return data.get("messages", []) if isinstance(data, dict) else []
