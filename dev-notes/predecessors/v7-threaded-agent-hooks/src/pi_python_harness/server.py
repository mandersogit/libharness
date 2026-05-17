"""Async local bridge server used by the TypeScript Pi extension."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import inspect
import secrets
import threading
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
    """Token-protected localhost JSONL bridge for Python tools.

    If ``tool_executor`` is supplied, every tool execution is moved into that
    executor. This is the normal ``PiAgentHarness`` posture: all harnesses share
    one tool thread pool, while each harness keeps its own registry and bridge
    server. Manifest requests remain on the asyncio I/O loop because they are
    cheap metadata reads.
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
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.timeout_ms = timeout_ms
        self.tool_executor = tool_executor
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
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
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

    async def __aenter__(self) -> "PythonToolServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve_client(reader, writer))
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
            await self._write_response(
                writer, request, success=False, error=f"incomplete bridge request: {exc}"
            )
        except Exception as exc:
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
            await self._write_response(writer, request, data=self.registry.manifest())
            return
        if req_type != "execute":
            raise ToolError(f"unknown bridge request type {req_type!r}")
        await self._dispatch_execute(request, reader, writer)

    async def _dispatch_execute(
        self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        tool_name = str(request.get("tool", ""))
        tool_call_id = str(request.get("toolCallId") or request.get("id") or "python-tool-call")
        params = request.get("params") or {}
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
            metadata = dict(request.get("context") or {})
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
