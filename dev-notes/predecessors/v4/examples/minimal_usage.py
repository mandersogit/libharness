from __future__ import annotations

import asyncio

from pi_python_harness import PiLaunchOptions, PiPythonHarness, ToolRegistry, ToolResult

registry = ToolRegistry()


@registry.tool(prompt_snippet="Run a Python-authored addition tool")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b), details={"a": a, "b": b, "sum": a + b})


async def main() -> None:
    # Set provider/model/API key as you normally would for Pi.
    # The command can be ["pi"] for an installed binary or ["node", "/path/to/dist/cli.js"].
    launch = PiLaunchOptions(
        command=("pi",),
        model="anthropic/claude-sonnet-4-5",
        no_session=True,
        no_extensions=True,
        no_builtin_tools=False,
    )

    async with PiPythonHarness(registry, launch_options=launch) as pi:
        pi.on_event(lambda event: print("event:", event.get("type")))
        await pi.prompt("Use the add tool to compute 41 + 1")


if __name__ == "__main__":
    asyncio.run(main())
