import asyncio
import json

import pytest

from pi_python_harness.bridge import PythonToolServer
from pi_python_harness.jsonl import StrictJsonlBuffer, dumps_line
from pi_python_harness.tools import ToolContext, ToolRegistry, ToolResult, ToolUpdate


@pytest.mark.asyncio
async def test_registry_manifest_and_tool_server_call():
    registry = ToolRegistry()

    @registry.register(
        name="add",
        label="Add",
        description="Add two integers",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        prompt_snippet="Add two integers with Python.",
    )
    def add(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.text(str(args["a"] + args["b"]), details={"tool_call_id": ctx.tool_call_id})

    manifest = registry.manifest()
    assert manifest["protocolVersion"] == 1
    assert manifest["tools"][0]["name"] == "add"
    assert manifest["tools"][0]["promptSnippet"] == "Add two integers with Python."

    async with PythonToolServer(registry) as server:
        endpoint = server.endpoint
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(
            dumps_line(
                {
                    "type": "call_tool",
                    "id": "r1",
                    "token": endpoint.token,
                    "toolName": "add",
                    "toolCallId": "tc1",
                    "args": {"a": 2, "b": 3},
                }
            )
        )
        await writer.drain()
        parser = StrictJsonlBuffer()
        records = []
        while not records:
            records.extend(parser.feed(await reader.read(4096)))
        writer.close()
        await writer.wait_closed()

    assert records[0]["type"] == "tool_result"
    assert records[0]["result"]["content"][0]["text"] == "5"
    assert records[0]["result"]["details"]["tool_call_id"] == "tc1"


@pytest.mark.asyncio
async def test_tool_server_streams_updates_before_result():
    registry = ToolRegistry()

    @registry.register(
        name="count",
        description="Stream counts",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def count(_args: dict, _ctx: ToolContext):
        yield ToolUpdate.text("one")
        yield ToolUpdate.text("two")
        yield ToolResult.text("done")

    async with PythonToolServer(registry) as server:
        endpoint = server.endpoint
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(
            dumps_line(
                {
                    "type": "call_tool",
                    "id": "r2",
                    "token": endpoint.token,
                    "toolName": "count",
                    "toolCallId": "tc2",
                    "args": {},
                }
            )
        )
        await writer.drain()
        parser = StrictJsonlBuffer()
        records = []
        while len(records) < 3:
            records.extend(parser.feed(await reader.read(4096)))
        writer.close()
        await writer.wait_closed()

    assert [r["type"] for r in records] == ["tool_update", "tool_update", "tool_result"]
    assert records[0]["partialResult"]["content"][0]["text"] == "one"
    assert records[2]["result"]["content"][0]["text"] == "done"
