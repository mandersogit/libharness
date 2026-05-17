from __future__ import annotations

import time

from pi_python_harness import Agent, AgentEvent, PiLaunchConfig, ToolContext, ToolRegistry, ToolResult

registry = ToolRegistry()


@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b), details={"a": a, "b": b})


@registry.register(description="Echo a message with a progress update")
async def echo(message: str, ctx: ToolContext) -> ToolResult:
    await ctx.update(f"received: {message}")
    return ToolResult.text(f"echo: {message}")


class BasicAgent(Agent):
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._started_at = 0.0
        super().__init__(*args, **kwargs)

    def on_agent_start(self, event: AgentEvent) -> None:
        _ = event
        self._started_at = time.monotonic()
        print("agent started")

    def on_agent_end(self, event: AgentEvent) -> None:
        _ = event
        elapsed = time.monotonic() - self._started_at if self._started_at else 0.0
        print(f"agent ended after {elapsed:.2f}s")


if __name__ == "__main__":
    config = PiLaunchConfig(no_builtin_tools=True)
    agent = BasicAgent(registry, config=config)
    try:
        agent.start()
        print(agent.get_state())
    finally:
        agent.close()
