from __future__ import annotations

import threading

import pytest

from pi_python_harness import Agent, AgentEvent, HarnessRuntime, ToolRegistry, UnhandledEventError
from tests.test_threaded_agent import fake_config


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
        runtime.run_async(agent._async_on_event({"type": "agent_start"}))
        assert seen == [(runtime.loop_thread_id, "PiAsyncioLoop-agent-test", "agent_start")]
    finally:
        agent.close()
        runtime.close()


def test_sync_hook_fires_on_dedicated_hook_executor_thread() -> None:
    runtime = HarnessRuntime(
        loop_thread_name="PiAsyncioLoop-sync-hook-test",
        hook_thread_name_prefix="dedicated-pi-hook",
    )
    seen: list[tuple[int, str, str]] = []

    class SyncHookAgent(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            seen.append((threading.get_ident(), threading.current_thread().name, event.type))

    agent = SyncHookAgent(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        runtime.run_async(agent._async_on_event({"type": "agent_start"}))
        assert len(seen) == 1
        thread_id, thread_name, event_type = seen[0]
        assert thread_id != runtime.loop_thread_id
        assert thread_name.startswith("dedicated-pi-hook")
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
        runtime.run_async(agent._async_on_event({"type": "agent_start"}))
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
            runtime.run_async(agent._async_on_event({"type": "agent_start"}))
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
