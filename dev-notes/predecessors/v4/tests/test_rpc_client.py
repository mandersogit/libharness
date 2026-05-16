from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pi_python_harness import PiRpcClient


async def main():
    fake = Path(__file__).with_name("fake_pi_rpc.py")
    client = PiRpcClient([sys.executable, str(fake), "--mode", "rpc"])
    await client.start()
    try:
        state = await client.get_state()
        assert state["sessionId"] == "fake-session"
        events = []
        client.on_event(events.append)
        await client.prompt("Say hello")
        # Let fake process events be read.
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "message_update" for e in events)
        await client.notify_extension_ui("ui-1", value="ok")
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "extension_ui_ack" for e in events)
    finally:
        await client.stop()


def test_rpc_client_against_fake_process():
    asyncio.run(main())
