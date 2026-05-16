from __future__ import annotations

import json
from urllib.request import Request, urlopen

from pi_python_harness import PythonToolBroker, ToolRegistry, ToolResult


def test_tool_schema_inference_and_invoke():
    registry = ToolRegistry()

    @registry.tool(prompt_snippet="Add two integers")
    def add(a: int, b: int) -> str:
        """Add two integers."""
        return str(a + b)

    manifest = registry.manifest()
    tool = manifest["tools"][0]
    assert tool["name"] == "add"
    assert tool["parameters"]["properties"]["a"]["type"] == "integer"
    assert tool["parameters"]["required"] == ["a", "b"]


def test_broker_execute_roundtrip():
    registry = ToolRegistry()

    @registry.tool()
    def greet(name: str, ctx) -> ToolResult:
        ctx.emit_update("working")
        return ToolResult.text(f"Hello, {name}!", details={"name": name})

    broker = PythonToolBroker(registry)
    handle = broker.start()
    try:
        payload = json.dumps({"toolName": "greet", "toolCallId": "t1", "arguments": {"name": "Ada"}}).encode()
        req = Request(
            handle.url + "/execute",
            data=payload,
            headers={"Content-Type": "application/json", "X-Pi-Python-Token": handle.token},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            records = [json.loads(line) for line in resp.read().decode().splitlines()]
        assert records[0]["type"] == "update"
        assert records[1]["type"] == "result"
        assert records[1]["result"]["content"][0]["text"] == "Hello, Ada!"
    finally:
        broker.stop()
