from __future__ import annotations

import asyncio
import json

from pi_python_harness.jsonl import dumps_line
from pi_python_harness.server import PythonToolServer
from pi_python_harness.tools import ToolContext, ToolRegistry, ToolResult


async def read_frame(reader: asyncio.StreamReader) -> dict:
    return json.loads((await reader.readline()).decode("utf-8"))


async def test_server_manifest_execute_and_updates() -> None:
    registry = ToolRegistry()

    @registry.register(description="Echo a message")
    async def echo(message: str, ctx: ToolContext) -> ToolResult:
        await ctx.update(f"working on {message}")
        return ToolResult.text(f"echo:{message}", details={"length": len(message)})

    server = PythonToolServer(registry)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(dumps_line({"id": "m1", "type": "manifest", "token": endpoint.token}))
        await writer.drain()
        manifest = await read_frame(reader)
        assert manifest["success"] is True
        assert manifest["data"]["protocolVersion"] == 1
        assert manifest["data"]["tools"][0]["name"] == "echo"
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(
            dumps_line(
                {
                    "id": "e1",
                    "type": "execute",
                    "token": endpoint.token,
                    "tool": "echo",
                    "params": {"message": "hi"},
                }
            )
        )
        await writer.drain()
        first = await read_frame(reader)
        second = await read_frame(reader)
        assert first["type"] == "update"
        assert "working on hi" in first["data"]["content"][0]["text"]
        assert second["type"] == "response"
        assert second["success"] is True
        assert second["data"]["content"][0]["text"] == "echo:hi"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
