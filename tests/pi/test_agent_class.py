from __future__ import annotations

import threading

import pytest
from tests.pi.test_threaded_agent import fake_config

from libharness.pi import Agent, AgentEvent, HarnessRuntime, ToolRegistry, UnhandledEventError


def test_init_subclass_rejects_both_async_and_sync_for_same_event() -> None:
    with pytest.raises(TypeError, match="both async_on_agent_start and on_agent_start"):

        class BadAgent(Agent):
            async def async_on_agent_start(self, event: AgentEvent) -> None:
                _ = event

            def on_agent_start(self, event: AgentEvent) -> None:
                _ = event


def test_init_subclass_rejects_unknown_hook_suffix() -> None:
    with pytest.raises(TypeError, match="unsupported event suffix"):

        class BadAgent(Agent):
            def on_not_a_pi_event(self, event: AgentEvent) -> None:
                _ = event


def test_async_hook_fires_on_runtime_loop_thread() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-agent-test")
    seen: list[tuple[int | None, str, str]] = []

    class AsyncHookAgent(Agent):
        async def async_on_agent_start(self, event: AgentEvent) -> None:
            seen.append((threading.get_ident(), threading.current_thread().name, event.type))

    agent = AsyncHookAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        assert seen == [(runtime.loop_thread_id, "PiAsyncioLoop-agent-test", "agent_start")]
    finally:
        agent.close()
        runtime.close()


def test_sync_hook_fires_on_owner_thread_not_loop() -> None:
    """F8 (post-Level-2): sync notification hooks run on the owner thread,
    routed through `_HookCall` on the harness command queue. There is no
    dedicated `hook_executor`; hooks are serialized with the harness's
    other operations naturally via the single-consumer queue."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-sync-hook-test")
    seen: list[tuple[int, str, str]] = []

    class SyncHookAgent(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            seen.append((threading.get_ident(), threading.current_thread().name, event.type))

    agent = SyncHookAgent(
        ToolRegistry(),
        config=fake_config(),
        runtime=runtime,
        owner_thread_name="PiAgentHarness-sync-hook-test",
    )
    try:
        agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        assert len(seen) == 1
        thread_id, thread_name, event_type = seen[0]
        assert thread_id != runtime.loop_thread_id, "must not run on the loop"
        assert thread_id == agent.owner_thread_id, "must run on the owner thread"
        assert thread_name == "PiAgentHarness-sync-hook-test"
        assert event_type == "agent_start"
    finally:
        agent.close()
        runtime.close()


def test_switch_color_subclass_clears_inherited_async_and_defines_sync() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-switch-color")
    seen: list[str] = []

    class A(Agent):
        async def async_on_agent_start(self, event: AgentEvent) -> None:
            seen.append(f"async:{event.type}")

    class B(A):
        async_on_agent_start = None

        def on_agent_start(self, event: AgentEvent) -> None:
            seen.append(f"sync:{event.type}")

    agent = B(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        assert seen == ["sync:agent_start"]
    finally:
        agent.close()
        runtime.close()


def test_strict_mode_raises_on_known_unhandled_event() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-strict")

    class StrictAgent(Agent):
        _raise_on_unhandled_event = True

    agent = StrictAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        with pytest.raises(UnhandledEventError, match="agent_start"):
            agent.pump_until(agent._async_on_event({"type": "agent_start"}))
    finally:
        agent.close()
        runtime.close()


def test_agent_start_installs_dispatcher_without_replacing_lower_level_subscribers() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-additive")
    hook_events: list[str] = []
    raw_events: list[str] = []

    class HookAgent(Agent):
        async def async_on_agent_end(self, event: AgentEvent) -> None:
            hook_events.append(event.type)

    agent = HookAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.start()
        agent.submit(
            lambda core: core.subscribe_client_events(
                lambda event: raw_events.append(event["type"])
            )
        ).result(timeout=5)
        agent.prompt_and_wait("hello", timeout=5)
        assert "agent_end" in hook_events
        assert "agent_end" in raw_events
    finally:
        agent.close()
        runtime.close()


def test_init_subclass_rejects_both_decision_flavors_for_same_event() -> None:
    with pytest.raises(TypeError, match="both async_decide_tool_call and decide_tool_call"):

        class BadDecisionAgent(Agent):
            async def async_decide_tool_call(self, event: AgentEvent) -> None:
                _ = event

            def decide_tool_call(self, event: AgentEvent) -> None:
                _ = event


def test_init_subclass_rejects_unknown_decision_suffix() -> None:
    with pytest.raises(TypeError, match="unsupported decision event suffix"):

        class BadDecisionAgent(Agent):
            def decide_not_a_pi_event(self, event: AgentEvent) -> None:
                _ = event


def test_extension_ui_request_is_skipped_even_in_strict_mode() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-extension-ui-skip")

    class StrictAgent(Agent):
        _raise_on_unhandled_event = True

    agent = StrictAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.pump_until(
            agent._async_on_event({"type": "extension_ui_request", "method": "confirm"})
        )
    finally:
        agent.close()
        runtime.close()


def test_strict_mode_accepts_all_declared_notification_events() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-strict-all")
    seen: list[str] = []

    def make_handler(name: str):
        def handler(self: Agent, event: AgentEvent) -> None:
            _ = self
            seen.append(event.type)
            assert event.type == name

        return handler

    attrs = {"_raise_on_unhandled_event": True}
    for event_name in Agent._EVENT_NAMES:
        attrs[f"on_{event_name}"] = make_handler(event_name)

    StrictAllAgent = type("StrictAllAgent", (Agent,), attrs)
    agent = StrictAllAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        for event_name in sorted(Agent._EVENT_NAMES):
            agent.pump_until(agent._async_on_event({"type": event_name}))
        assert set(seen) == set(Agent._EVENT_NAMES)
    finally:
        agent.close()
        runtime.close()


def test_notification_hook_exception_is_logged_and_subsequent_events_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-hook-exception")
    seen: list[str] = []

    class RaisingAgent(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            raise RuntimeError(f"bad {event.type}")

        def on_agent_end(self, event: AgentEvent) -> None:
            seen.append(event.type)

    agent = RaisingAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        agent.pump_until(agent._async_on_event({"type": "agent_end"}))
        assert seen == ["agent_end"]
        assert "Agent notification hook failed for event agent_start" in caplog.text
    finally:
        agent.close()
        runtime.close()


def test_agent_event_payload_is_shallow_immutable() -> None:
    event = AgentEvent.from_mapping({"type": "agent_start"})
    with pytest.raises(TypeError):
        event.payload["type"] = "agent_end"  # type: ignore[index]


# ---------------------------------------------------------------------------
# Item C — strict-mode E2E for ALL declared events (37 total).
#
# The plan's analysis doc (§ Items worth discussing C) noted that the
# `_raise_on_unhandled_event = True` strict-mode logic was exercised for
# unknown events and for the 18 RPC notification events
# (test_strict_mode_accepts_all_declared_notification_events above), but
# NOT end-to-end for the 19 decision events through `_async_on_bridge_event`.
# These two tests close that gap.
# ---------------------------------------------------------------------------


def test_strict_mode_accepts_all_decision_events_notify_only() -> None:
    """Strict mode must not raise UnhandledEventError when every decision event
    has an observation hook defined and is fired via the bridge in
    notify-only mode (require_decision=False)."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-strict-decision-notify")
    seen: list[str] = []

    def make_handler(name: str):
        def handler(self: Agent, event: AgentEvent) -> None:
            _ = self
            seen.append(event.type)
            assert event.type == name

        return handler

    attrs = {"_raise_on_unhandled_event": True}
    for event_name in Agent._DECISION_EVENT_NAMES:
        attrs[f"on_{event_name}"] = make_handler(event_name)

    StrictDecisionNotifyAgent = type("StrictDecisionNotifyAgent", (Agent,), attrs)
    agent = StrictDecisionNotifyAgent(
        ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False
    )
    try:
        cancelled = threading.Event()
        for event_name in sorted(Agent._DECISION_EVENT_NAMES):
            agent.pump_until(
                agent._async_on_bridge_event(
                    {"type": event_name}, cancelled, False, None
                )
            )
        assert set(seen) == set(Agent._DECISION_EVENT_NAMES)
    finally:
        agent.close()
        runtime.close()


def test_strict_mode_accepts_all_decision_events_with_decide() -> None:
    """Strict mode must not raise when every decision event has BOTH an
    observation hook (`on_<event>`) AND a decision hook (`decide_<event>`)
    defined and is fired via the bridge in decision-required mode."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-strict-decision-with-decide")
    notify_seen: list[str] = []
    decide_seen: list[str] = []

    def make_on_handler(name: str):
        def handler(self: Agent, event: AgentEvent) -> None:
            _ = self
            notify_seen.append(event.type)
            assert event.type == name

        return handler

    def make_decide_handler(name: str):
        def handler(self: Agent, event: AgentEvent) -> object | None:
            _ = self
            decide_seen.append(event.type)
            assert event.type == name
            return None

        return handler

    attrs = {"_raise_on_unhandled_event": True}
    for event_name in Agent._DECISION_EVENT_NAMES:
        attrs[f"on_{event_name}"] = make_on_handler(event_name)
        attrs[f"decide_{event_name}"] = make_decide_handler(event_name)

    StrictDecisionWithDecideAgent = type("StrictDecisionWithDecideAgent", (Agent,), attrs)
    agent = StrictDecisionWithDecideAgent(
        ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False
    )
    try:
        cancelled = threading.Event()
        for event_name in sorted(Agent._DECISION_EVENT_NAMES):
            agent.pump_until(
                agent._async_on_bridge_event(
                    {"type": event_name}, cancelled, True, f"req-{event_name}"
                )
            )
        assert set(notify_seen) == set(Agent._DECISION_EVENT_NAMES)
        assert set(decide_seen) == set(Agent._DECISION_EVENT_NAMES)
    finally:
        agent.close()
        runtime.close()


def test_strict_mode_raises_on_unknown_decision_event() -> None:
    """Strict mode must raise UnhandledEventError when a decision event arrives
    that's not in `_DECISION_EVENT_NAMES`."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-strict-decision-unknown")

    class StrictAgent(Agent):
        _raise_on_unhandled_event = True

    agent = StrictAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        cancelled = threading.Event()
        with pytest.raises(UnhandledEventError, match="not_a_real_event"):
            agent.pump_until(
                agent._async_on_bridge_event(
                    {"type": "not_a_real_event"}, cancelled, False, None
                )
            )
    finally:
        agent.close()
        runtime.close()
