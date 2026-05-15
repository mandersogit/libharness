"""Regression test for the v5 set_model wire-shape bug.

v5 of the harness shipped ``set_model`` as
``{"type": "set_model", "provider": ..., "model": ...}``, but pi's
RPC contract requires ``modelId`` instead of ``model`` (see
``packages/coding-agent/src/modes/rpc/rpc-types.ts:31`` in pi-mono).
This test pins the wire shape so the bug cannot quietly reappear.
"""

from __future__ import annotations

import sys
from pathlib import Path

from libharness.pi.rpc import PiLaunchConfig, PiRpcClient


def _fake_pi_config() -> PiLaunchConfig:
    script = Path(__file__).with_name("fake_pi_rpc.py")
    return PiLaunchConfig(
        pi_command=[sys.executable, str(script)],
        request_timeout=5,
        offline=False,
        no_extensions=False,
        no_skills=False,
        no_prompt_templates=False,
        no_context_files=False,
    )


async def test_set_model_uses_modelId_field() -> None:
    """set_model must serialize the model identifier under ``modelId``."""
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.set_model("openai-codex", "gpt-5.5")

    received = (response.get("data") or {}).get("received") or {}
    assert received.get("type") == "set_model"
    assert received.get("provider") == "openai-codex"
    assert received.get("modelId") == "gpt-5.5", (
        "set_model must send the value as `modelId` (pi RPC contract). "
        "v5 sent it as `model` and pi silently failed; this test pins the fix."
    )
    assert "model" not in received, (
        "set_model must not also send a `model` key — pi's RPC type union "
        "rejects unknown fields"
    )
