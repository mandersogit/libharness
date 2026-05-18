from __future__ import annotations

import json
import socket

from tests.pi.test_threaded_agent import fake_config

from libharness.pi import Agent, AgentEvent, HarnessRuntime, ToolRegistry
from libharness.pi.jsonl import dumps_line


def test_bridge_decision_events_do_not_reach_rpc_on_event_subscribers() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-channel-separation")
    raw_events: list[str] = []
    decisions: list[str] = []

    class GateAgent(Agent):
        def decide_tool_call(self, event: AgentEvent) -> dict[str, bool]:
            decisions.append(event.type)
            return {"block": False}

    agent = GateAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.start()
        agent.submit(
            lambda core: core.subscribe_client_events(
                lambda event: raw_events.append(event["type"])
            )
        ).result(timeout=5)
        endpoint = agent.snapshot().bridge_endpoint
        assert endpoint is not None
        with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as sock:
            sock.sendall(
                dumps_line(
                    {
                        "id": "d1",
                        "type": "event",
                        "token": endpoint.token,
                        "event": "tool_call",
                        "data": {"type": "tool_call", "toolName": "bash"},
                    }
                )
            )
            response = json.loads(sock.makefile("rb").readline().decode("utf-8"))
        assert response["success"] is True
        assert decisions == ["tool_call"]
        assert "tool_call" not in raw_events
    finally:
        agent.close()
        runtime.close()
