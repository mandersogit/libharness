from __future__ import annotations

import asyncio

from pi_python_harness import PiLaunchConfig, PiPythonHarness, ToolContext, ToolRegistry, ToolResult

registry = ToolRegistry()


@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b), details={"a": a, "b": b})


@registry.register(description="Echo a message with a progress update")
async def echo(message: str, ctx: ToolContext) -> ToolResult:
    await ctx.update(f"received: {message}")
    return ToolResult.text(f"echo: {message}")


async def main() -> None:
    config = PiLaunchConfig(no_builtin_tools=True)
    async with PiPythonHarness(registry, config=config) as harness:
        state = await harness.client.get_state()
        print(state)


if __name__ == "__main__":
    asyncio.run(main())
