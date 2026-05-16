"""Python tool server consumed by the generated TypeScript Pi extension."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .jsonl import StrictJsonlBuffer, dumps_line
from .tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolUpdate,
    is_async_generator,
    is_sync_generator,
    maybe_await,
    normalize_tool_result,
)

BridgeRequest = dict[str, Any]
BridgeResponse = dict[str, Any]


@dataclass(frozen=True)
class BridgeEndpoint:
    host: str
    port: int
    token: str

    def env(self) -> dict[str, str]:
        return {
            "PI_PY_BRIDGE_HOST": self.host,
            "PI_PY_BRIDGE_PORT": str(self.port),
            "PI_PY_BRIDGE_TOKEN": self.token,
        }


class PythonToolServer:
    """Async TCP JSONL server exposing Python tools to the TS shim.

    The server intentionally binds to loopback by default and requires a random
    bearer token. It is intended to be launched by the Python parent process just
    before starting ``pi --mode rpc``.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.on_error = on_error
        self._server: asyncio.base_events.Server | None = None

    @property
    def endpoint(self) -> BridgeEndpoint:
        if self._server is None:
            raise RuntimeError("server is not started")
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("server has no listening sockets")
        actual_host, actual_port = sockets[0].getsockname()[:2]
        return BridgeEndpoint(host=str(actual_host), port=int(actual_port), token=self.token)

    async def start(self) -> BridgeEndpoint:
        if self._server is not None:
            return self.endpoint
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        return self.endpoint

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def __aenter__(self) -> "PythonToolServer":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_one_request(reader)
            await self._dispatch(request, writer)
        except Exception as exc:  # noqa: BLE001 - bridge must report errors to caller
            if self.on_error:
                self.on_error(exc)
            fallback_id = None
            with contextlib.suppress(Exception):
                fallback_id = locals().get("request", {}).get("id")
            await self._send(
                writer,
                {
                    "type": "tool_error",
                    "id": fallback_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_one_request(self, reader: asyncio.StreamReader) -> BridgeRequest:
        parser = StrictJsonlBuffer()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                raise ConnectionError("client disconnected before sending a request")
            records = parser.feed(chunk)
            if records:
                request = records[0]
                if not isinstance(request, dict):
                    raise ValueError("bridge request must be a JSON object")
                return request

    async def _dispatch(self, request: BridgeRequest, writer: asyncio.StreamWriter) -> None:
        if request.get("token") != self.token:
            await self._send(writer, {"type": "tool_error", "id": request.get("id"), "error": "unauthorized"})
            return

        request_type = request.get("type")
        if request_type == "list_tools":
            await self._send(writer, {"type": "tools", "id": request.get("id"), "manifest": self.registry.manifest()})
            return

        if request_type != "call_tool":
            await self._send(
                writer,
                {"type": "tool_error", "id": request.get("id"), "error": f"unsupported request type: {request_type}"},
            )
            return

        await self._call_tool(request, writer)

    async def _call_tool(self, request: BridgeRequest, writer: asyncio.StreamWriter) -> None:
        request_id = request.get("id")
        tool_name = str(request["toolName"])
        tool_call_id = str(request.get("toolCallId") or request_id or "")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("tool args must be a JSON object")

        tool = self.registry.get(tool_name)
        ctx = ToolContext(tool_call_id=tool_call_id, tool_name=tool_name)
        raw_result = tool.handler(args, ctx)

        if is_async_generator(raw_result):
            final: ToolResult | None = None
            async for item in raw_result:
                maybe_final = await self._handle_tool_item(request_id, item, writer)
                if maybe_final is not None:
                    final = maybe_final
            await self._send_final(writer, request_id, final or ToolResult.text("Done"))
            return

        if is_sync_generator(raw_result):
            final = None
            for item in raw_result:
                maybe_final = await self._handle_tool_item(request_id, item, writer)
                if maybe_final is not None:
                    final = maybe_final
            await self._send_final(writer, request_id, final or ToolResult.text("Done"))
            return

        result = normalize_tool_result(await maybe_await(raw_result))
        await self._send_final(writer, request_id, result)

    async def _handle_tool_item(
        self,
        request_id: str | None,
        item: ToolUpdate | ToolResult | str | dict[str, Any],
        writer: asyncio.StreamWriter,
    ) -> ToolResult | None:
        if isinstance(item, ToolUpdate):
            await self._send(writer, {"type": "tool_update", "id": request_id, "partialResult": item.to_wire()})
            return None
        if isinstance(item, ToolResult):
            return item
        # Strings and dicts in a generator are treated as progress updates, not final results.
        update = ToolUpdate.text(item) if isinstance(item, str) else ToolUpdate(content=item.get("content", []), details=item.get("details", {}))
        await self._send(writer, {"type": "tool_update", "id": request_id, "partialResult": update.to_wire()})
        return None

    async def _send_final(self, writer: asyncio.StreamWriter, request_id: str | None, result: ToolResult) -> None:
        await self._send(writer, {"type": "tool_result", "id": request_id, "result": result.to_wire()})

    async def _send(self, writer: asyncio.StreamWriter, response: BridgeResponse) -> None:
        writer.write(dumps_line(response))
        await writer.drain()
