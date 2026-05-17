from __future__ import annotations

import sys
from pathlib import Path

from pi_python_harness.rpc import PiLaunchConfig, PiRpcClient


async def test_rpc_client_handles_headless_ui_and_events() -> None:
    script = Path(__file__).with_name("fake_pi_rpc.py")
    client = PiRpcClient(
        PiLaunchConfig(
            pi_command=[sys.executable, str(script)],
            request_timeout=5,
            offline=False,
            no_extensions=False,
            no_skills=False,
            no_prompt_templates=False,
            no_context_files=False,
        )
    )
    async with client:
        state = await client.get_state()
        assert state["sessionId"] == "fake"
        events = await client.prompt_and_wait("hello", timeout=5)
        assert any(event.get("type") == "agent_end" for event in events)
