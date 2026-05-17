from __future__ import annotations

from pi_python_harness import Agent, AgentEvent, HookContext, PiLaunchConfig, ToolRegistry

registry = ToolRegistry()


class PermissionGateAgent(Agent):
    """Block destructive bash commands before Pi executes them."""

    def on_tool_call(self, event: AgentEvent) -> None:
        print(f"tool requested: {event.get('toolName')}")

    def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> dict[str, object] | None:
        if ctx.cancelled:
            return None
        if event.get("toolName") != "bash":
            return None
        input_payload = event.get("input", {})
        command = input_payload.get("command", "") if isinstance(input_payload, dict) else ""
        if "rm -rf" in command:
            return {
                "block": True,
                "reason": "permission gate blocked a destructive bash command",
            }
        return None


if __name__ == "__main__":
    config = PiLaunchConfig(no_builtin_tools=False)
    agent = PermissionGateAgent(registry, config=config)
    try:
        agent.start()
        agent.prompt_and_wait("List the current directory, but do not delete anything.", timeout=120)
    finally:
        agent.close()
