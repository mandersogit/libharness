from __future__ import annotations

import sys
from pathlib import Path

from pi_python_harness import PiLaunchConfig, PiRpcClient


async def test_set_model_uses_model_id_wire_shape() -> None:
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
        response = await client.set_model("fake-provider", "fake-model")
        assert response["success"] is True
        assert response["data"] == {"provider": "fake-provider", "id": "fake-model"}
