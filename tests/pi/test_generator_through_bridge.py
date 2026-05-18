"""End-to-end regression tests for generator-yielding tool handlers.

`libharness.pi.tools.collect_tool_result` supports two flavors of streaming
handler: synchronous generators (`yield ...`) and async generators
(`async def ...: yield ...`). Each yielded value is forwarded over the bridge
as a wire-level ``update`` frame via `ToolContext.update`; the **last** yielded
value also becomes the data payload of the final ``response`` frame.

The unit test surface (``tests/pi/test_tools.py``) exercises the registry/
schema layer but does not run handlers through the real ``PythonToolServer``
TCP wire. These tests pin the generator path end-to-end so that future
refactors of the dispatcher, server, or bridge protocol cannot silently
regress streaming behavior.

Pattern follows ``tests/pi/test_server.py::test_server_manifest_execute_and_updates``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

from libharness.pi.jsonl import dumps_line
from libharness.pi.server import PythonToolServer
from libharness.pi.tools import ToolRegistry


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    return json.loads((await reader.readline()).decode("utf-8"))


async def test_sync_generator_streams_updates_then_response() -> None:
    """A sync-generator handler yielding N values produces N update frames + 1 response."""

    registry = ToolRegistry()

    @registry.register(description="Stream three steps then finish")
    def stream_steps(label: str) -> Iterator[str]:
        yield f"step 1:{label}"
        yield f"step 2:{label}"
        yield f"step 3:{label}"
        yield f"done:{label}"

    server = PythonToolServer(registry)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(
            dumps_line(
                {
                    "id": "g-sync-1",
                    "type": "execute",
                    "token": endpoint.token,
                    "tool": "stream_steps",
                    "params": {"label": "hi"},
                }
            )
        )
        await writer.drain()

        # Four yields => four update frames over the wire, in order.
        # `collect_tool_result` calls `ctx.update(result)` once per yielded
        # value, including the last one (which also becomes the response).
        expected_texts = [
            "step 1:hi",
            "step 2:hi",
            "step 3:hi",
            "done:hi",
        ]
        for expected in expected_texts:
            frame = await _read_frame(reader)
            assert frame["type"] == "update", frame
            assert frame["id"] == "g-sync-1"
            assert frame["data"]["content"][0]["text"] == expected

        # Final frame is the response; data mirrors the last yielded value.
        final = await _read_frame(reader)
        assert final["type"] == "response", final
        assert final["id"] == "g-sync-1"
        assert final["success"] is True
        assert final["data"]["content"][0]["text"] == "done:hi"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


async def test_async_generator_streams_updates_then_response() -> None:
    """An async-generator handler streams the same way as a sync generator."""

    registry = ToolRegistry()

    @registry.register(description="Async stream two steps then finish")
    async def astream(label: str) -> AsyncIterator[str]:
        # Touch the event loop between yields so we exercise the async path,
        # not just an immediately-drained iterator.
        yield f"a-step 1:{label}"
        await asyncio.sleep(0)
        yield f"a-step 2:{label}"
        await asyncio.sleep(0)
        yield f"a-done:{label}"

    server = PythonToolServer(registry)
    endpoint = await server.start()
    try:
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        writer.write(
            dumps_line(
                {
                    "id": "g-async-1",
                    "type": "execute",
                    "token": endpoint.token,
                    "tool": "astream",
                    "params": {"label": "yo"},
                }
            )
        )
        await writer.drain()

        expected_texts = ["a-step 1:yo", "a-step 2:yo", "a-done:yo"]
        for expected in expected_texts:
            frame = await _read_frame(reader)
            assert frame["type"] == "update", frame
            assert frame["id"] == "g-async-1"
            assert frame["data"]["content"][0]["text"] == expected

        final = await _read_frame(reader)
        assert final["type"] == "response", final
        assert final["id"] == "g-async-1"
        assert final["success"] is True
        assert final["data"]["content"][0]["text"] == "a-done:yo"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
