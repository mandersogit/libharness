import sys
from pathlib import Path

import pytest

from pi_python_harness.client import PiRpcClient


@pytest.mark.asyncio
async def test_rpc_client_with_fake_pi_process(tmp_path: Path):
    fake = tmp_path / "fake_pi.py"
    fake.write_text(
        """
import json, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    if msg.get('type') == 'get_state':
        sys.stdout.write(json.dumps({'id': msg.get('id'), 'type': 'response', 'command': 'get_state', 'success': True, 'data': {'isStreaming': False}}) + '\\n')
        sys.stdout.flush()
    elif msg.get('type') == 'prompt':
        sys.stdout.write(json.dumps({'id': msg.get('id'), 'type': 'response', 'command': 'prompt', 'success': True}) + '\\n')
        sys.stdout.write(json.dumps({'type': 'agent_end', 'messages': []}) + '\\n')
        sys.stdout.flush()
""".strip(),
        encoding="utf-8",
    )

    client = PiRpcClient(command=[sys.executable, str(fake)], no_session=True)
    async with client:
        state = await client.get_state()
        assert state["isStreaming"] is False
        await client.prompt("hello")
        event = await client.wait_for_event("agent_end", timeout=1)
        assert event["type"] == "agent_end"
