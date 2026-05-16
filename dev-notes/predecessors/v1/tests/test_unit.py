import asyncio
import json
import socket

from py_pi_harness import PythonToolServer, ToolContext, ToolRegistry, ToolResult


def test_registry_schema_inference_and_manifest():
    registry = ToolRegistry()

    @registry.tool(description="Echo tool")
    def echo(text: str, times: int = 1) -> str:
        return text * times

    manifest = registry.manifest()
    assert manifest[0]["name"] == "echo"
    assert manifest[0]["parameters"]["properties"]["text"]["type"] == "string"
    assert manifest[0]["parameters"]["properties"]["times"]["type"] == "integer"
    assert manifest[0]["parameters"]["required"] == ["text"]


def test_registry_executes_context_updates():
    registry = ToolRegistry()
    seen = []

    @registry.tool(description="Streaming tool")
    async def stream(text: str, ctx: ToolContext) -> ToolResult:
        await ctx.update(f"partial:{text}")
        return ToolResult.text(f"final:{text}")

    async def run():
        async def cb(result):
            seen.append(result.to_frame())
        result = await registry.execute("stream", {"text": "x"}, ToolContext("call-1", cb))
        return result

    result = asyncio.run(run())
    assert seen[0]["content"][0]["text"] == "partial:x"
    assert result.content[0]["text"] == "final:x"


def _send(host, port, frame):
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(json.dumps(frame).encode() + b"\n")
        lines = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            lines.extend([line for line in data.split(b"\n") if line])
        return [json.loads(line.decode()) for line in lines]


def test_python_tool_server_manifest_and_execute():
    registry = ToolRegistry()

    @registry.tool(description="Add two integers")
    def add(a: int, b: int) -> int:
        return a + b

    async def run():
        server = PythonToolServer(registry)
        await server.start()
        try:
            host, port = server.address
            manifest = await asyncio.to_thread(_send, host, port, {"id": "m", "type": "manifest", "token": server.token})
            result = await asyncio.to_thread(
                _send,
                host,
                port,
                {"id": "x", "type": "execute", "token": server.token, "tool": "add", "toolCallId": "call", "params": {"a": 2, "b": 3}},
            )
            return manifest, result
        finally:
            await server.stop()

    manifest, result = asyncio.run(run())
    assert manifest[0]["success"] is True
    assert manifest[0]["data"]["tools"][0]["name"] == "add"
    assert result[0]["success"] is True
    assert result[0]["data"]["details"] == 5
