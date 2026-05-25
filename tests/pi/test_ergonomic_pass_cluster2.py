"""Regression tests for ergonomic-pass cluster 2: validation tightening.

Covers A.3/F10 (keyword-only ctx passed by keyword, not positional),
F11 (mutable class-default `_decision_timeouts_ms` is now MappingProxyType),
F12 (Agent.__init__ raises on owned-kwarg overlap), F22 (sync notification hook
returning an awaitable is rejected), F23 (timeout validator rejects bool / float),
F25 (constructor-passed `decision_timeouts_ms` validated in PiAgentHarness).

Sources: `dev-notes/2026-05-17-v8-port-deferred-items.md` § A.3, F11, F12, F22,
F23, F25.
"""

from __future__ import annotations

import threading
from types import MappingProxyType

import pytest

from libharness.pi import (
    Agent,
    AgentEvent,
    HarnessRuntime,
    HookContext,
    PiAgentHarness,
    ToolRegistry,
)
from libharness.pi.agent_class import _call_decision_handler, _handler_ctx_mode
from libharness.pi.hook_surface import AgentHookSurface, _is_positive_int

# ---------------------------------------------------------------------------
# A.3 / F10: keyword-only ctx parameter is passed by keyword.
# ---------------------------------------------------------------------------


def test_handler_ctx_mode_detects_keyword_only_ctx() -> None:
    """The dispatcher must distinguish positional-vs-keyword ctx passage."""

    def positional_ctx(event, ctx):  # type: ignore[no-untyped-def]
        return (event, ctx)

    def keyword_only_ctx(event, *, ctx):  # type: ignore[no-untyped-def]
        return (event, ctx)

    def event_only(event):  # type: ignore[no-untyped-def]
        return event

    assert _handler_ctx_mode(positional_ctx) == (True, None)
    assert _handler_ctx_mode(keyword_only_ctx) == (True, "ctx")
    assert _handler_ctx_mode(event_only) == (False, None)


def test_call_decision_handler_passes_keyword_only_ctx_by_keyword() -> None:
    """Pre-fix: `def decide_X(event, *, ctx)` raised TypeError because the
    dispatcher invoked positionally. Post-fix: ctx is passed via kwargs.
    """

    def handler(event, *, ctx):  # type: ignore[no-untyped-def]
        return ("ok", event["type"], ctx.event_name)

    event = AgentEvent.from_mapping({"type": "tool_call"})
    ctx = HookContext("tool_call", request_id="r1")
    result = _call_decision_handler(handler, event, ctx)
    assert result == ("ok", "tool_call", "tool_call")


# ---------------------------------------------------------------------------
# F11: _decision_timeouts_ms default is read-only at the base class.
# ---------------------------------------------------------------------------


def test_base_decision_timeouts_ms_is_immutable() -> None:
    """Mutating the base class default must raise; users must reassign
    instead of mutating to avoid leaking timeouts across sibling subclasses.
    """
    assert isinstance(AgentHookSurface._decision_timeouts_ms, MappingProxyType)
    with pytest.raises(TypeError):
        AgentHookSurface._decision_timeouts_ms["tool_call"] = 5000  # type: ignore[index]


def test_subclass_can_reassign_decision_timeouts_ms() -> None:
    """Subclass reassigning `_decision_timeouts_ms` is the canonical path
    and must work."""

    class TimedAgent(Agent):
        _decision_timeouts_ms = {"tool_call": 5000}  # type: ignore[assignment]

        def decide_tool_call(self, event: AgentEvent) -> object | None:
            _ = event
            return None

    # Sanity: validation accepts the reassignment.
    assert TimedAgent._decision_timeouts_ms.get("tool_call") == 5000


# ---------------------------------------------------------------------------
# F12: Agent.__init__ raises on owned-kwarg overlap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owned_kwarg",
    ["bridge_event_handler", "initial_open_gates", "decision_timeouts_ms"],
)
def test_agent_init_rejects_owned_kwargs(owned_kwarg: str) -> None:
    """Passing any of the three Agent-owned constructor kwargs raises
    TypeError. Pre-fix: silently overwritten."""

    class _A(Agent):
        pass

    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-F12")
    try:
        # Build a kwargs dict with the owned key set to anything sensible.
        kw = {owned_kwarg: None}
        with pytest.raises(TypeError, match="Agent owns these constructor kwargs"):
            _A(ToolRegistry(), runtime=runtime, threaded=False, **kw)  # type: ignore[arg-type]
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# F22: sync notification hook returning an awaitable raises.
# ---------------------------------------------------------------------------


def test_sync_notification_hook_returning_awaitable_logged() -> None:
    """Pre-fix: `run_in_executor` returned the coroutine object and
    discarded it unawaited (silent hook never ran). Post-fix: the
    dispatcher checks `inspect.isawaitable` after the executor call and
    raises TypeError, which gets logged via the usual notification
    exception path.
    """
    import logging

    from libharness.pi.agent import PiLaunchConfig

    fired: list[str] = []

    class BadAgent(Agent):
        def on_agent_start(self, event: AgentEvent):  # type: ignore[no-untyped-def]
            # Return a coroutine — wrong for a sync hook.
            async def coro() -> None:
                fired.append("would-have-run")

            return coro()

    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-F22")
    config = PiLaunchConfig(pi_command=["echo"])
    agent = BadAgent(ToolRegistry(), config=config, runtime=runtime, threaded=False)
    try:
        # caplog catches the TypeError logged by the notification dispatcher.
        import io
        import logging as _logging

        buf = io.StringIO()
        handler = _logging.StreamHandler(buf)
        handler.setLevel(_logging.ERROR)
        logger = _logging.getLogger("libharness.pi.agent_class")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(_logging.ERROR)
        try:
            agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        out = buf.getvalue()
        assert "must not" in out or "awaitable" in out, (
            f"expected TypeError about awaitable in log, got: {out!r}"
        )
        assert fired == [], "the unawaited coroutine must not have run"
    finally:
        agent.close()
        runtime.close()
        _ = logging  # silence unused-import lint if logging branch is removed


# ---------------------------------------------------------------------------
# F23: strict-int timeout validator rejects bool / float.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "should_pass"),
    [
        (1, True),
        (5000, True),
        (0, False),
        (-1, False),
        (True, False),
        (False, False),
        (0.5, False),
        (1.0, False),
        ("5000", False),
    ],
)
def test_is_positive_int_strict(value: object, should_pass: bool) -> None:
    assert _is_positive_int(value) is should_pass


def test_decision_timeouts_validator_rejects_bool() -> None:
    with pytest.raises(TypeError, match="positive int"):

        class BoolTimeoutAgent(Agent):
            _decision_timeouts_ms = {"tool_call": True}  # type: ignore[dict-item]

            def decide_tool_call(self, event: AgentEvent) -> object | None:
                _ = event
                return None


def test_decision_timeouts_validator_rejects_float() -> None:
    with pytest.raises(TypeError, match="positive int"):

        class FloatTimeoutAgent(Agent):
            _decision_timeouts_ms = {"tool_call": 0.5}  # type: ignore[dict-item]

            def decide_tool_call(self, event: AgentEvent) -> object | None:
                _ = event
                return None


# ---------------------------------------------------------------------------
# F25: constructor-passed decision_timeouts_ms validated in PiAgentHarness.
# ---------------------------------------------------------------------------


def test_pi_agent_harness_validates_constructor_decision_timeouts_ms() -> None:
    """Direct PiAgentHarness construction with a malformed timeouts dict
    must raise at __init__ time, not silently store the bad value."""
    with pytest.raises(TypeError, match="positive int"):
        PiAgentHarness(
            decision_timeouts_ms={"tool_call": -100},
            start_owner_thread=False,
        )


def test_pi_agent_harness_validates_constructor_decision_timeouts_ms_bool() -> None:
    """Bool values (which pre-fix were accepted via `value > 0` since True == 1)
    must now raise."""
    with pytest.raises(TypeError, match="positive int"):
        PiAgentHarness(
            decision_timeouts_ms={"tool_call": True},  # type: ignore[dict-item]
            start_owner_thread=False,
        )


def test_pi_agent_harness_accepts_valid_decision_timeouts_ms() -> None:
    """Sanity: a valid timeouts dict at construction time still works."""
    harness = PiAgentHarness(
        decision_timeouts_ms={"tool_call": 5000},
        start_owner_thread=False,
    )
    # The dict was deep-copied into _core_kwargs; verify it's there.
    assert harness._core_kwargs["decision_timeouts_ms"]["tool_call"] == 5000

    # Thread-related state — _check_owner doesn't fire because we're on
    # whatever-thread (threaded=False is implicit because start_owner_thread=False
    # but threaded defaults to True, so we're a proxy without an owner).
    # Just verify no exception during construction.
    _ = threading.current_thread()
