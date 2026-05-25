from __future__ import annotations

import concurrent.futures
import json
import socket
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest
from tests.pi.test_threaded_agent import fake_config

from libharness.pi import Agent, AgentEvent, HarnessRuntime, HookContext, ToolRegistry
from libharness.pi.jsonl import dumps_line
from libharness.pi.server import BridgeEndpoint, PythonToolServer


def _event_request(
    agent: Agent,
    endpoint: BridgeEndpoint,
    event_name: str,
    data: Mapping[str, Any] | None = None,
    *,
    request_id: str = "event-1",
    expect_response: bool = True,
) -> dict[str, Any] | None:
    """Send a bridge event request; pump the harness queue while waiting.

    F8/Level-3-Option-A: the socket I/O happens on a worker thread so
    MainThread is free to consume `_HookCall` messages from the harness
    command queue (via `agent.pump_until`). Without this, sync hooks
    fired by the bridge during the request would deadlock — MainThread
    blocked on `sock.recv`, no queue consumer.
    """
    response_future: concurrent.futures.Future[dict[str, Any] | None] = (
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
                            "id": request_id,
                            "type": "event",
                            "token": endpoint.token,
                            "event": event_name,
                            "data": {"type": event_name, **dict(data or {})},
                        }
                    )
                )
                if not expect_response:
                    response_future.set_result(None)
                    return
                file = sock.makefile("rb")
                line = file.readline()
                assert line
                response_future.set_result(json.loads(line.decode("utf-8")))
        except BaseException as exc:
            response_future.set_exception(exc)

    threading.Thread(target=_worker, name="event-request-worker", daemon=True).start()
    return agent.pump_until(response_future)


def _notify_request(
    endpoint: BridgeEndpoint,
    event_name: str,
    data: Mapping[str, Any] | None = None,
) -> None:
    with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as sock:
        sock.sendall(
            dumps_line(
                {
                    "id": "notify-1",
                    "type": "notify_event",
                    "token": endpoint.token,
                    "event": event_name,
                    "data": {"type": event_name, **dict(data or {})},
                }
            )
        )


def _start_bridge(runtime: HarnessRuntime, agent: Agent) -> tuple[PythonToolServer, BridgeEndpoint]:
    server = PythonToolServer(
        ToolRegistry(),
        event_handler=agent._async_on_bridge_event,
        initial_open_gates=tuple(type(agent)._initial_open_gates_for_class()),
        decision_timeouts_ms=type(agent)._decision_timeouts_for_manifest(),
    )
    endpoint = runtime.run_async(server.start())
    return server, endpoint


def test_sync_decision_hook_runs_on_owner_thread_and_returns_result() -> None:
    """F8 (post-Level-2): sync decision hooks run on the owner thread,
    routed through `_HookCall` on the harness command queue. The hook's
    return value is propagated back to the bridge dispatch via the
    completion future."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-sync")
    seen: list[tuple[str, str, bool]] = []

    class GateAgent(Agent):
        def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> dict[str, object]:
            seen.append((threading.current_thread().name, event.type, ctx.cancelled))
            return {"block": True, "reason": "blocked by Python"}

    agent = GateAgent(
        ToolRegistry(),
        config=fake_config(),
        runtime=runtime,
        owner_thread_name="PiAgentHarness-decision-sync",
    )
    server, endpoint = _start_bridge(runtime, agent)
    try:
        response = _event_request(agent, endpoint, "tool_call", {"toolName": "bash"})
        assert response is not None
        assert response["success"] is True
        assert response["data"] == {"block": True, "reason": "blocked by Python"}
        assert seen == [(seen[0][0], "tool_call", False)]
        assert seen[0][0] == "PiAgentHarness-decision-sync"
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_async_decision_hook_runs_on_runtime_loop_and_provider_payload_shape() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-async")
    seen: list[tuple[int | None, str, str]] = []

    class PayloadAgent(Agent):
        async def async_decide_before_provider_request(
            self, event: AgentEvent, ctx: HookContext
        ) -> dict[str, object]:
            seen.append((threading.get_ident(), threading.current_thread().name, ctx.event_name))
            payload = dict(event["payload"])
            payload["temperature"] = 0
            return payload

    agent = PayloadAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        response = _event_request(
            agent,
            endpoint,
            "before_provider_request",
            {"payload": {"messages": [], "temperature": 1}},
        )
        assert response is not None
        assert response["success"] is True
        assert response["data"] == {"messages": [], "temperature": 0}
        assert seen == [
            (runtime.loop_thread_id, "PiAsyncioLoop-decision-async", "before_provider_request")
        ]
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_session_before_decision_return_shape() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-session-before")

    class SessionAgent(Agent):
        def decide_session_before_switch(
            self, event: AgentEvent, ctx: HookContext
        ) -> dict[str, bool]:
            _ = event, ctx
            return {"cancel": True}

    agent = SessionAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        response = _event_request(agent, endpoint, "session_before_switch", {"reason": "new"})
        assert response is not None
        assert response["success"] is True
        assert response["data"] == {"cancel": True}
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_decision_hook_exception_is_logged_and_bridge_call_fails_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-error")

    class ErrorAgent(Agent):
        def decide_tool_call(self, event: AgentEvent) -> None:
            raise RuntimeError(f"boom for {event.type}")

    agent = ErrorAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        response = _event_request(agent, endpoint, "tool_call", {"toolName": "bash"})
        assert response is not None
        assert response["success"] is False
        assert "boom for tool_call" in response["error"]
        assert "Agent decision hook failed for event tool_call" in caplog.text

        # The server remains alive after the failed decision request.
        with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as sock:
            sock.sendall(dumps_line({"id": "m", "type": "manifest", "token": endpoint.token}))
            line = sock.makefile("rb").readline()
            assert json.loads(line.decode("utf-8"))["success"] is True
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_decision_timeout_opt_in_is_advertised_in_manifest() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-timeout")

    class TimeoutAgent(Agent):
        _decision_timeouts_ms = {"tool_call": 25}

        def decide_tool_call(self, event: AgentEvent) -> None:
            _ = event
            time.sleep(1)
            return None

    agent = TimeoutAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as sock:
            sock.sendall(dumps_line({"id": "m", "type": "manifest", "token": endpoint.token}))
            manifest = json.loads(sock.makefile("rb").readline().decode("utf-8"))
        assert manifest["data"]["initialOpenGates"] == ["tool_call"]
        assert manifest["data"]["decisionTimeoutsMs"] == {"tool_call": 25}
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_cancellation_mid_decision_when_bridge_connection_closes() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-cancel")
    exited = threading.Event()

    class CancellableAgent(Agent):
        def decide_session_before_switch(self, event: AgentEvent, ctx: HookContext) -> None:
            _ = event
            deadline = time.monotonic() + 2.0
            while not ctx.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            if ctx.cancelled:
                exited.set()
            return None

    agent = CancellableAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=2.0) as sock:
            sock.sendall(
                dumps_line(
                    {
                        "id": "drop",
                        "type": "event",
                        "token": endpoint.token,
                        "event": "session_before_switch",
                        "data": {"type": "session_before_switch", "reason": "new"},
                    }
                )
            )
        # Pump the harness queue while waiting for the hook (running on
        # the owner thread / MainThread pump) to detect cancellation
        # and set `exited`. Without pumping, the _HookCall would sit
        # forever and ctx.cancelled would never propagate.
        assert agent.pump_until(exited, timeout=2.0)
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_bridge_connection_drop_mid_decision_does_not_deadlock() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-drop")
    entered = threading.Event()
    exited = threading.Event()

    class DropAgent(Agent):
        def decide_tool_call(self, event: AgentEvent, ctx: HookContext) -> None:
            _ = event
            entered.set()
            while not ctx.cancelled:
                time.sleep(0.01)
            exited.set()
            return None

    # threaded=True so the sync hook's busy-wait runs on the owner thread,
    # leaving MainThread free to close the socket while the hook polls
    # ctx.cancelled. In threaded=False, MainThread *is* the hook runner —
    # a blocking hook would starve MainThread of the chance to close().
    agent = DropAgent(
        ToolRegistry(),
        config=fake_config(),
        runtime=runtime,
        owner_thread_name="PiAgentHarness-drop",
    )
    server, endpoint = _start_bridge(runtime, agent)
    try:
        sock = socket.create_connection((endpoint.host, endpoint.port), timeout=2.0)
        sock.sendall(
            dumps_line(
                {
                    "id": "drop-tool",
                    "type": "event",
                    "token": endpoint.token,
                    "event": "tool_call",
                    "data": {"type": "tool_call", "toolName": "bash"},
                }
            )
        )
        assert entered.wait(2.0)
        sock.close()
        assert exited.wait(2.0)
        runtime.run_async(async_noop())
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_concurrent_decision_events_are_serialized_by_owner_thread() -> None:
    """F8 (post-Level-2): concurrent sync decision events fire FIFO via
    the owner thread's command queue. The single-consumer queue
    guarantees no two sync hooks run concurrently for the same harness."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-concurrent")
    active = 0
    max_active = 0
    lock = threading.Lock()
    seen: list[int] = []

    class SerialAgent(Agent):
        def decide_tool_call(self, event: AgentEvent) -> dict[str, int]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            seen.append(int(event["index"]))
            with lock:
                active -= 1
            return {"index": int(event["index"])}

    agent = SerialAgent(
        ToolRegistry(),
        config=fake_config(),
        runtime=runtime,
        owner_thread_name="PiAgentHarness-decision-concurrent",
    )
    server, endpoint = _start_bridge(runtime, agent)
    responses: list[dict[str, Any] | None] = []

    def send(index: int) -> None:
        responses.append(
            _event_request(agent, endpoint, "tool_call", {"index": index}, request_id=str(index))
        )

    try:
        t1 = threading.Thread(target=send, args=(1,))
        t2 = threading.Thread(target=send, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        assert len(responses) == 2
        assert all(response and response["success"] for response in responses)
        assert max_active == 1
        assert sorted(seen) == [1, 2]
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_notification_and_decision_for_same_bridge_event_run_in_order() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-decision-order")
    order: list[tuple[str, str]] = []
    observed_event_ids: list[int] = []

    class ObserveAndDecideAgent(Agent):
        def on_tool_call(self, event: AgentEvent) -> None:
            order.append(("observe", event.type))
            observed_event_ids.append(id(event))

        def decide_tool_call(self, event: AgentEvent) -> dict[str, bool]:
            order.append(("decide", event.type))
            observed_event_ids.append(id(event))
            return {"block": False}

    agent = ObserveAndDecideAgent(
        ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False
    )
    server, endpoint = _start_bridge(runtime, agent)
    try:
        response = _event_request(agent, endpoint, "tool_call", {"toolName": "bash"})
        assert response is not None
        assert response["success"] is True
        assert order == [("observe", "tool_call"), ("decide", "tool_call")]
        assert observed_event_ids[0] == observed_event_ids[1]
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


def test_notify_event_fire_and_forget_reaches_observation_hook() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-notify-event")
    observed = threading.Event()

    class NotifyAgent(Agent):
        def on_tool_call(self, event: AgentEvent) -> None:
            if event.type == "tool_call":
                observed.set()

    agent = NotifyAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    server, endpoint = _start_bridge(runtime, agent)
    try:
        _notify_request(endpoint, "tool_call", {"toolName": "bash"})
        assert agent.pump_until(observed, timeout=2.0)
    finally:
        runtime.run_async(server.close())
        agent.close()
        runtime.close()


async def async_noop() -> None:
    return None
