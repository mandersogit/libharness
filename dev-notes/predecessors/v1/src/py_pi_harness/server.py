from __future__ import annotations

import asyncio
import json
import secrets
import traceback
import typing as t
from dataclasses import dataclass

from .tools import ToolContext, ToolRegistry, ToolResult

JsonObject = dict[str, t.Any]


@dataclass
class PythonToolServer:
    """Async JSONL server exposing a ToolRegistry to the TypeScript shim."""

    registry: ToolRegistry
    host: str = "127.0.0.1"
    port: int = 0
    token: str | None = None

    def __post_init__(self) -> None:
        if self.token is None:
            self.token = secrets.token_urlsafe(24)
        self._server: asyncio.AbstractServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return (self.host, self.port)
        sock = self._server.sockets[0]
        host, port = sock.getsockname()[:2]
        return (str(host), int(port))

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self.host, self.port = self.address

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            if not line.endswith(b"\n"):
                await self._write_response(writer, None, False, error="Protocol error: command must be LF-delimited")
                return
            payload = json.loads(line.decode("utf-8").rstrip("\n").rstrip("\r"))
            await self._dispatch(payload, writer)
        except Exception as exc:  # defensive: the shim expects a response frame
            await self._write_response(writer, None, False, error=f"Python tool server failure: {exc}")
        finally:
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, payload: JsonObject, writer: asyncio.StreamWriter) -> None:
        request_id = payload.get("id")
        if payload.get("token") != self.token:
            await self._write_response(writer, request_id, False, error="Unauthorized Python tool bridge request")
            return

        kind = payload.get("type")
        if kind == "manifest":
            await self._write_response(writer, request_id, True, data={"tools": self.registry.manifest()})
            return

        if kind == "execute":
            tool_name = payload.get("tool")
            params = payload.get("params") or {}
            tool_call_id = str(payload.get("toolCallId") or request_id or "")

            async def send_update(result: ToolResult) -> None:
                await self._write_json(writer, {"id": request_id, "type": "update", "data": result.to_frame()})

            try:
                ctx = ToolContext(tool_call_id=tool_call_id, update_callback=send_update)
                result = await self.registry.execute(str(tool_name), params, ctx)
                await self._write_response(writer, request_id, True, data=result.to_frame())
            except Exception as exc:
                await self._write_response(
                    writer,
                    request_id,
                    False,
                    error=str(exc),
                    data={"traceback": traceback.format_exc(limit=20)},
                )
            return

        await self._write_response(writer, request_id, False, error=f"Unknown bridge request type: {kind!r}")

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        request_id: str | None,
        success: bool,
        *,
        data: t.Any = None,
        error: str | None = None,
    ) -> None:
        frame: JsonObject = {"id": request_id, "type": "response", "success": success}
        if success:
            frame["data"] = data
        else:
            frame["error"] = error or "error"
            if data is not None:
                frame["data"] = data
        await self._write_json(writer, frame)

    async def _write_json(self, writer: asyncio.StreamWriter, frame: JsonObject) -> None:
        writer.write(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()
