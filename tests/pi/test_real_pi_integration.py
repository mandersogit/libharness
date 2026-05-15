from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from libharness.pi import PiLaunchConfig, PiPythonHarness, ToolContext, ToolRegistry, ToolResult


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("PI_CLI"), reason="set PI_CLI to a Pi CLI executable or dist/cli.js for real integration")
async def test_real_pi_runs_bridge_and_python_tool_loop() -> None:
    pi_cli = Path(os.environ["PI_CLI"])
    command = [sys.executable, str(pi_cli)] if pi_cli.suffix == ".py" else (["node", str(pi_cli)] if pi_cli.suffix == ".js" else [str(pi_cli)])
    registry = ToolRegistry()

    @registry.register(description="Return a tagged echo")
    async def echo(message: str = "", ctx: ToolContext | None = None) -> ToolResult:
        await ctx.update("echo starting") if ctx else None
        return ToolResult.text(f"PYTHON_ECHO:{message}")

    config = PiLaunchConfig(pi_command=command, request_timeout=90, startup_timeout=5, no_builtin_tools=True)
    async with PiPythonHarness(
        registry,
        config=config,
        fake_provider=True,
        fake_tool_name="echo",
        fake_tool_args={"message": "integration"},
        fake_final_text="integration complete",
    ) as harness:
        state = await harness.client.get_state()
        assert state.get("sessionId")
        commands = await harness.client.get_commands()
        assert any(cmd.get("name") == "py-tools" for cmd in commands)
        events = await harness.client.prompt_and_wait("call the Python echo tool", timeout=90)
        assert any(event.get("type") == "agent_end" for event in events)
