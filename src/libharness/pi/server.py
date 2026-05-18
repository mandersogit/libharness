"""Async local bridge server used by the TypeScript Pi extension."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import inspect
import logging
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .jsonl import StrictJsonlDecoder, dumps_line
from .tools import (
    ToolContext,
    ToolError,
    ToolRegistry,
    collect_tool_result,
    exception_to_wire,
    normalize_tool_value,
)

_LOG = logging.getLogger(__name__)
BridgeEventHandler = Callable[
    [Mapping[str, Any], threading.Event, bool, str | None], Awaitable[object | None]
]


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    host: str
    port: int
    token: str
    timeout_ms: int = 120_000

    def env(self, *, prefix: str = "PI_PY") -> dict[str, str]:
        return {
            f"{prefix}_TOOLS_HOST": self.host,
            f"{prefix}_TOOLS_PORT": str(self.port),
            f"{prefix}_TOOLS_TOKEN": self.token,
            f"{prefix}_BRIDGE_TIMEOUT_MS": str(self.timeout_ms),
        }


class PythonToolServer:
    """Token-protected localhost JSONL bridge for Python tools and events.

    If ``tool_executor`` is supplied, every tool execution is moved into that
    executor. This is the normal ``PiAgentHarness`` posture: all harnesses share
    one tool thread pool, while each harness keeps its own registry and bridge
    server. Manifest requests remain on the asyncio I/O loop because they are
    cheap metadata reads.

    ``event_handler`` is the optional Agent bridge hook entrypoint. It handles
    ``notify_event`` and ``event`` bridge requests from the TypeScript shim.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        timeout_ms: int = 120_000,
        tool_executor: concurrent.futures.Executor | None = None,
        event_handler: BridgeEventHandler | None = None,
        initial_open_gates: Sequence[str] = (),
        decision_timeouts_ms: Mapping[str, int] | None = None,
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.timeout_ms = timeout_ms
        self.tool_executor = tool_executor
        self.event_handler = event_handler
        self.initial_open_gates = tuple(initial_open_gates)
        self.decision_timeouts_ms = dict(decision_timeouts_ms or {})
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def endpoint(self) -> BridgeEndpoint:
        if self._server is None:
            raise RuntimeError("PythonToolServer is not started")
        sockets = cast(Any, self._server).sockets
        if not sockets:
            raise RuntimeError("PythonToolServer has no listening sockets")
        host, port = sockets[0].getsockname()[:2]
        return BridgeEndpoint(
            host=str(host),
            port=int(port),
            token=self.token,
            timeout_ms=self.timeout_ms,
        )

    async def start(self) -> BridgeEndpoint:
        if self._server is not None:
            return self.endpoint
        # Match StrictJsonlDecoder's max_buffer_bytes (8 MiB). The default
        # StreamReader limit is 64 KiB; without this, real-world bridge frames
        # carrying large payloads (e.g., compaction message arrays, large tool
        # argument blobs) hit LimitOverrunError on readuntil and silently
        # disappear into _serve_client's exception handler.
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            limit=8 * 1024 * 1024,
        )
        return self.endpoint

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._tasks:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def __aenter__(self) -> PythonToolServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve_client(reader, writer))
        self._track_task(task)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        decoder = StrictJsonlDecoder(max_buffer_bytes=8 * 1024 * 1024)
        request: dict[str, Any] | None = None
        try:
            chunk = await asyncio.wait_for(
                reader.readuntil(b"\n"), timeout=max(1.0, self.timeout_ms / 1000.0)
            )
            records = decoder.feed(chunk)
            if len(records) != 1 or not isinstance(records[0], dict):
                raise ToolError("expected exactly one JSON object request")
            request = records[0]
            if not hmac.compare_digest(str(request.get("token", "")), self.token):
                raise ToolError("invalid bridge token")
            await self._dispatch(request, reader, writer)
        except asyncio.IncompleteReadError as exc:
            if request is not None:
                await self._write_response(
                    writer, request, success=False, error=f"incomplete bridge request: {exc}"
                )
        except Exception as exc:
            if request is None:
                # Malformed-frame failures (bad JSON, oversized frame, decode
                # error, timeout) leave request=None. notify_event-style
                # silence on the wire is the right contract, but the operator
                # gets no signal without an explicit log here.
                _LOG.warning("malformed bridge request: %s", exc, exc_info=True)
            elif request.get("type") != "notify_event":
                await self._write_response(
                    writer,
                    request,
                    success=False,
                    error=str(exc),
                    data=exception_to_wire(exc).get("details"),
                )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        req_type = request.get("type")
        if req_type == "manifest":
            manifest = self.registry.manifest()
            manifest["initialOpenGates"] = list(self.initial_open_gates)
            manifest["decisionTimeoutsMs"] = dict(self.decision_timeouts_ms)
            await self._write_response(writer, request, data=manifest)
            return
        if req_type == "execute":
            await self._dispatch_execute(request, reader, writer)
            return
        if req_type == "notify_event":
            task = asyncio.create_task(self._dispatch_bridge_event(request, reader, wait=False))
            self._track_task(task)
            return
        if req_type == "event":
            result = await self._dispatch_bridge_event(request, reader, wait=True)
            await self._write_response(writer, request, data=result)
            return
        raise ToolError(f"unknown bridge request type {req_type!r}")

    async def _dispatch_bridge_event(
        self, request: dict[str, Any], reader: asyncio.StreamReader, *, wait: bool
    ) -> object | None:
        if self.event_handler is None:
            return None
        event_name = str(request.get("event") or "")
        # Match the params-validation pattern from _dispatch_execute: don't let
        # `or {}` coerce falsy non-dict values into {} before the isinstance
        # check runs. Treat missing / None as {} and reject everything else.
        data = request.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ToolError("event.data must be an object")
        event = dict(data)
        # The bridge envelope's `event` field is authoritative — it's the name
        # the TS shim subscribed under and the gate the Python side opens. If
        # the inner data carries a different type, the shim or an attacker is
        # confusing the dispatcher; reject loudly. Envelope wins unconditionally.
        inner_type = event.get("type")
        if inner_type is not None and inner_type != event_name:
            raise ToolError(
                f"bridge envelope event={event_name!r} does not match "
                f"data.type={inner_type!r}"
            )
        event["type"] = event_name
        cancelled = threading.Event()

        async def watch_disconnect() -> None:
            try:
                while not reader.at_eof():
                    data_chunk = await reader.read(1)
                    if data_chunk == b"":
                        break
            except Exception:
                pass
            finally:
                cancelled.set()

        watcher = asyncio.create_task(watch_disconnect())
        try:
            if wait:
                return await self.event_handler(event, cancelled, True, _request_id(request))
            try:
                await self.event_handler(event, cancelled, False, _request_id(request))
            except Exception:
                _LOG.exception("Bridge notify_event handler failed for event %s", event_name)
            return None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _dispatch_execute(
        self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        tool_name = str(request.get("tool", ""))
        tool_call_id = str(request.get("toolCallId") or request.get("id") or "python-tool-call")
        # Don't use `or {}` to default missing params — that silently coerces
        # falsy non-dict values ([], 0, "", False) into {} before the isinstance
        # check runs. Treat missing / None as {} and reject everything else.
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ToolError("execute.params must be an object")

        cancelled = threading.Event()
        loop = asyncio.get_running_loop()

        async def send_update_async(value: Any) -> None:
            result = normalize_tool_value(value).to_wire()
            await self._write_frame(
                writer, {"id": request.get("id"), "type": "update", "data": result}
            )

        def send_update_threadsafe(value: Any) -> None:
            future = asyncio.run_coroutine_threadsafe(send_update_async(value), loop)
            future.result(timeout=max(1.0, self.timeout_ms / 1000.0))

        async def send_update_direct(value: Any) -> None:
            await send_update_async(value)

        async def watch_disconnect() -> None:
            try:
                while not reader.at_eof():
                    data = await reader.read(1)
                    if data == b"":
                        break
            except Exception:
                pass
            finally:
                cancelled.set()

        watcher = asyncio.create_task(watch_disconnect())
        try:
            # Same falsy-coercion guard as params/data above. `context` is
            # informational metadata but the bug-class is identical.
            context_obj = request.get("context", {})
            if context_obj is None:
                context_obj = {}
            if not isinstance(context_obj, dict):
                raise ToolError("execute.context must be an object")
            metadata = dict(context_obj)
            metadata.setdefault("toolExecutor", bool(self.tool_executor))
            if self.tool_executor is None:
                ctx = ToolContext(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    cwd=request.get("cwd"),
                    metadata=metadata,
                    _cancelled=cancelled,
                    _update_callback=send_update_direct,
                )
                result = await self._execute_tool_async(tool_name, params, ctx)
            else:
                ctx = ToolContext(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    cwd=request.get("cwd"),
                    metadata=metadata,
                    _cancelled=cancelled,
                    _update_callback=send_update_threadsafe,
                )
                result = await loop.run_in_executor(
                    self.tool_executor, self._execute_tool_blocking, tool_name, params, ctx
                )
            await self._write_response(writer, request, data=result.to_wire())
        except asyncio.CancelledError:
            cancelled.set()
            await self._write_response(
                writer, request, success=False, error="tool execution cancelled"
            )
        except Exception as exc:
            wire = exception_to_wire(exc)
            await self._write_response(
                writer, request, success=False, error=wire["error"], data=wire["details"]
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _execute_tool_async(
        self, tool_name: str, params: dict[str, Any], ctx: ToolContext
    ) -> Any:
        registered = self.registry.get(tool_name)
        raw = registered.call(params, ctx)
        return await collect_tool_result(raw, ctx)

    def _execute_tool_blocking(
        self, tool_name: str, params: dict[str, Any], ctx: ToolContext
    ) -> Any:
        registered = self.registry.get(tool_name)
        raw = registered.call(params, ctx)
        if inspect.isawaitable(raw) or inspect.isasyncgen(raw):
            return asyncio.run(collect_tool_result(raw, ctx))
        if inspect.isgenerator(raw):
            return asyncio.run(collect_tool_result(raw, ctx))
        return normalize_tool_value(raw)

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        request: dict[str, Any] | None,
        *,
        success: bool = True,
        data: Any = None,
        error: str | None = None,
    ) -> None:
        frame: dict[str, Any] = {
            "id": request.get("id") if request else None,
            "type": "response",
            "success": success,
        }
        if success:
            frame["data"] = data
        else:
            frame["error"] = error or "bridge request failed"
            if data is not None:
                frame["data"] = data
        await self._write_frame(writer, frame)

    async def _write_frame(self, writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
        writer.write(dumps_line(frame))
        await writer.drain()


def _request_id(request: Mapping[str, Any]) -> str | None:
    request_id = request.get("id")
    return str(request_id) if request_id is not None else None
