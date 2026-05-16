from __future__ import annotations

from py_pi_harness import PiPythonHarness, ToolContext, ToolRegistry, ToolResult

registry = ToolRegistry()


@registry.tool(description="Echo text back to Pi")
def echo(text: str) -> str:
    return text


@registry.tool(description="Add two integers")
def add(a: int, b: int) -> int:
    return a + b


@registry.tool(description="Demonstrate streaming partial updates")
async def progress(message: str, ctx: ToolContext) -> ToolResult:
    await ctx.update(f"started: {message}")
    await ctx.update(f"almost done: {message}")
    return ToolResult.text(f"done: {message}")


if __name__ == "__main__":
    # Replace pi_command/cwd with a source checkout command during development:
    # pi_command=["node", "/path/to/pi/packages/coding-agent/dist/cli.js"], cwd="/path/to/pi"
    harness = PiPythonHarness(registry, pi_command=["pi"])
    harness.start()
    try:
        client = harness.client
        assert client is not None
        print(client.get_state())
        print(client.get_commands())
    finally:
        harness.close()
