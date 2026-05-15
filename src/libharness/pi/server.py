"""Async local bridge server used by the TypeScript Pi extension."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

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
    """Token-protected localhost JSONL bridge for Python tools."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        timeout_ms: int = 120_000,
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.timeout_ms = timeout_ms
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def endpoint(self) -> BridgeEndpoint:
        if self._server is None:
            raise RuntimeError("PythonToolServer is not started")
        sock = self._server.sockets[0]
        host, port = sock.getsockname()[:2]
        return BridgeEndpoint(host=str(host), port=int(port), token=self.token, timeout_ms=self.timeout_ms)

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

    async def __aenter__(self) -> PythonToolServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve_client(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        decoder = StrictJsonlDecoder(max_buffer_bytes=8 * 1024 * 1024)
        request: dict[str, Any] | None = None
        try:
            chunk = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=max(1.0, self.timeout_ms / 1000.0))
            records = decoder.feed(chunk)
            if len(records) != 1 or not isinstance(records[0], dict):
                raise ToolError("expected exactly one JSON object request")
            request = records[0]
            if not hmac.compare_digest(str(request.get("token", "")), self.token):
                raise ToolError("invalid bridge token")
            await self._dispatch(request, reader, writer)
        except asyncio.IncompleteReadError as exc:
            await self._write_response(writer, request, success=False, error=f"incomplete bridge request: {exc}")
        except Exception as exc:
            await self._write_response(writer, request, success=False, error=str(exc), data=exception_to_wire(exc).get("details"))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req_type = request.get("type")
        if req_type == "manifest":
            await self._write_response(writer, request, data=self.registry.manifest())
            return
        if req_type != "execute":
            raise ToolError(f"unknown bridge request type {req_type!r}")

        tool_name = str(request.get("tool", ""))
        tool_call_id = str(request.get("toolCallId") or request.get("id") or "python-tool-call")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise ToolError("execute.params must be an object")
        registered = self.registry.get(tool_name)
        cancelled = asyncio.Event()

        async def send_update(value: Any) -> None:
            result = normalize_tool_value(value).to_wire()
            await self._write_frame(writer, {"id": request.get("id"), "type": "update", "data": result})

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
            ctx = ToolContext(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                cwd=request.get("cwd"),
                metadata=dict(request.get("context") or {}),
                _cancelled=cancelled,
                _update_callback=send_update,
            )
            raw = registered.call(params, ctx)
            result = await collect_tool_result(raw, ctx)
            await self._write_response(writer, request, data=result.to_wire())
        except asyncio.CancelledError:
            await self._write_response(writer, request, success=False, error="tool execution cancelled")
        except Exception as exc:
            wire = exception_to_wire(exc)
            await self._write_response(writer, request, success=False, error=wire["error"], data=wire["details"])
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        request: dict[str, Any] | None,
        *,
        success: bool = True,
        data: Any = None,
        error: str | None = None,
    ) -> None:
        frame: dict[str, Any] = {"id": request.get("id") if request else None, "type": "response", "success": success}
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
