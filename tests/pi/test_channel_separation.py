from __future__ import annotations

import concurrent.futures
import json
import socket
import threading
from typing import Any

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
        # F8/Level-3-Option-A: do the bridge socket I/O on a worker thread so
        # MainThread is free to pump the harness queue (sync decision hook
        # dispatch needs the pump consumer when threaded=False).
        response_future: concurrent.futures.Future[dict[str, Any]] = (
            concurrent.futures.Future()
        )

        def _worker() -> None:
            try:
                with socket.create_connection(
                    (endpoint.host, endpoint.port), timeout=2.0
                ) as sock:
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
                    response_future.set_result(
                        json.loads(sock.makefile("rb").readline().decode("utf-8"))
                    )
            except BaseException as exc:
                response_future.set_exception(exc)

        threading.Thread(target=_worker, name="bridge-io", daemon=True).start()
        response = agent.pump_until(response_future)
        assert response["success"] is True
        assert decisions == ["tool_call"]
        assert "tool_call" not in raw_events
    finally:
        agent.close()
        runtime.close()
