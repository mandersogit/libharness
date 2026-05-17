"""Declarative hook surface for the :class:`libharness.pi.Agent` class.

Splits the *what hooks exist* layer (this file) from the *dispatch logic*
layer (``agent_class.py``). Two different reasons to change, two different
classes — the ``Agent`` class body becomes readable without ~120 lines of
static ClassVar declarations in front of every method.

The mixin is public (``AgentHookSurface``, no leading underscore) per
author resolution #2 in ``dev-notes/2026-05-17-v8-analysis.md`` §
Resolutions. Third-party code may inspect or subclass it directly.

Validation classmethods live here because they only read class-level
state from the surface they validate. They run from ``Agent``'s
``__init_subclass__`` via MRO lookup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from .events import AgentEvent

AsyncAgentHook = Callable[[AgentEvent], Awaitable[object]]
SyncAgentHook = Callable[[AgentEvent], object]
AsyncDecisionHook = Callable[..., Awaitable[object | None]]
SyncDecisionHook = Callable[..., object | None]


class AgentHookSurface:

    _EVENT_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "agent_start",
            "agent_end",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "message_end",
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
            "queue_update",
            "compaction_start",
            "compaction_end",
            "session_info_changed",
            "thinking_level_changed",
            "auto_retry_start",
            "auto_retry_end",
            "extension_error",
        }
    )
    _DECISION_EVENT_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "resources_discover",
            "session_start",
            "session_before_switch",
            "session_before_fork",
            "session_before_compact",
            "session_compact",
            "session_shutdown",
            "session_before_tree",
            "session_tree",
            "context",
            "before_provider_request",
            "after_provider_response",
            "before_agent_start",
            "model_select",
            "thinking_level_select",
            "tool_call",
            "tool_result",
            "user_bash",
            "input",
        }
    )
    _OBSERVABLE_EVENT_NAMES: ClassVar[frozenset[str]] = _EVENT_NAMES | _DECISION_EVENT_NAMES
    _RPC_REQUEST_TYPES: ClassVar[frozenset[str]] = frozenset({"extension_ui_request"})

    _raise_on_unhandled_event: ClassVar[bool] = False
    _decision_timeout_ms: ClassVar[int | None] = None
    _decision_timeouts_ms: ClassVar[dict[str, int]] = {}

    # Sync notification hooks (37: 18 RPC notification + 19 decision-event observation slots)
    on_agent_start: ClassVar[SyncAgentHook | None] = None
    on_agent_end: ClassVar[SyncAgentHook | None] = None
    on_turn_start: ClassVar[SyncAgentHook | None] = None
    on_turn_end: ClassVar[SyncAgentHook | None] = None
    on_message_start: ClassVar[SyncAgentHook | None] = None
    on_message_update: ClassVar[SyncAgentHook | None] = None
    on_message_end: ClassVar[SyncAgentHook | None] = None
    on_tool_execution_start: ClassVar[SyncAgentHook | None] = None
    on_tool_execution_update: ClassVar[SyncAgentHook | None] = None
    on_tool_execution_end: ClassVar[SyncAgentHook | None] = None
    on_queue_update: ClassVar[SyncAgentHook | None] = None
    on_compaction_start: ClassVar[SyncAgentHook | None] = None
    on_compaction_end: ClassVar[SyncAgentHook | None] = None
    on_session_info_changed: ClassVar[SyncAgentHook | None] = None
    on_thinking_level_changed: ClassVar[SyncAgentHook | None] = None
    on_auto_retry_start: ClassVar[SyncAgentHook | None] = None
    on_auto_retry_end: ClassVar[SyncAgentHook | None] = None
    on_extension_error: ClassVar[SyncAgentHook | None] = None
    on_resources_discover: ClassVar[SyncAgentHook | None] = None
    on_session_start: ClassVar[SyncAgentHook | None] = None
    on_session_before_switch: ClassVar[SyncAgentHook | None] = None
    on_session_before_fork: ClassVar[SyncAgentHook | None] = None
    on_session_before_compact: ClassVar[SyncAgentHook | None] = None
    on_session_compact: ClassVar[SyncAgentHook | None] = None
    on_session_shutdown: ClassVar[SyncAgentHook | None] = None
    on_session_before_tree: ClassVar[SyncAgentHook | None] = None
    on_session_tree: ClassVar[SyncAgentHook | None] = None
    on_context: ClassVar[SyncAgentHook | None] = None
    on_before_provider_request: ClassVar[SyncAgentHook | None] = None
    on_after_provider_response: ClassVar[SyncAgentHook | None] = None
    on_before_agent_start: ClassVar[SyncAgentHook | None] = None
    on_model_select: ClassVar[SyncAgentHook | None] = None
    on_thinking_level_select: ClassVar[SyncAgentHook | None] = None
    on_tool_call: ClassVar[SyncAgentHook | None] = None
    on_tool_result: ClassVar[SyncAgentHook | None] = None
    on_user_bash: ClassVar[SyncAgentHook | None] = None
    on_input: ClassVar[SyncAgentHook | None] = None

    # Sync decision hooks (19; one per decision event)
    decide_resources_discover: ClassVar[SyncDecisionHook | None] = None
    decide_session_start: ClassVar[SyncDecisionHook | None] = None
    decide_session_before_switch: ClassVar[SyncDecisionHook | None] = None
    decide_session_before_fork: ClassVar[SyncDecisionHook | None] = None
    decide_session_before_compact: ClassVar[SyncDecisionHook | None] = None
    decide_session_compact: ClassVar[SyncDecisionHook | None] = None
    decide_session_shutdown: ClassVar[SyncDecisionHook | None] = None
    decide_session_before_tree: ClassVar[SyncDecisionHook | None] = None
    decide_session_tree: ClassVar[SyncDecisionHook | None] = None
    decide_context: ClassVar[SyncDecisionHook | None] = None
    decide_before_provider_request: ClassVar[SyncDecisionHook | None] = None
    decide_after_provider_response: ClassVar[SyncDecisionHook | None] = None
    decide_before_agent_start: ClassVar[SyncDecisionHook | None] = None
    decide_model_select: ClassVar[SyncDecisionHook | None] = None
    decide_thinking_level_select: ClassVar[SyncDecisionHook | None] = None
    decide_tool_call: ClassVar[SyncDecisionHook | None] = None
    decide_tool_result: ClassVar[SyncDecisionHook | None] = None
    decide_user_bash: ClassVar[SyncDecisionHook | None] = None
    decide_input: ClassVar[SyncDecisionHook | None] = None

    # Async notification hooks (37: mirrors the sync block)
    async_on_agent_start: ClassVar[AsyncAgentHook | None] = None
    async_on_agent_end: ClassVar[AsyncAgentHook | None] = None
    async_on_turn_start: ClassVar[AsyncAgentHook | None] = None
    async_on_turn_end: ClassVar[AsyncAgentHook | None] = None
    async_on_message_start: ClassVar[AsyncAgentHook | None] = None
    async_on_message_update: ClassVar[AsyncAgentHook | None] = None
    async_on_message_end: ClassVar[AsyncAgentHook | None] = None
    async_on_tool_execution_start: ClassVar[AsyncAgentHook | None] = None
    async_on_tool_execution_update: ClassVar[AsyncAgentHook | None] = None
    async_on_tool_execution_end: ClassVar[AsyncAgentHook | None] = None
    async_on_queue_update: ClassVar[AsyncAgentHook | None] = None
    async_on_compaction_start: ClassVar[AsyncAgentHook | None] = None
    async_on_compaction_end: ClassVar[AsyncAgentHook | None] = None
    async_on_session_info_changed: ClassVar[AsyncAgentHook | None] = None
    async_on_thinking_level_changed: ClassVar[AsyncAgentHook | None] = None
    async_on_auto_retry_start: ClassVar[AsyncAgentHook | None] = None
    async_on_auto_retry_end: ClassVar[AsyncAgentHook | None] = None
    async_on_extension_error: ClassVar[AsyncAgentHook | None] = None
    async_on_resources_discover: ClassVar[AsyncAgentHook | None] = None
    async_on_session_start: ClassVar[AsyncAgentHook | None] = None
    async_on_session_before_switch: ClassVar[AsyncAgentHook | None] = None
    async_on_session_before_fork: ClassVar[AsyncAgentHook | None] = None
    async_on_session_before_compact: ClassVar[AsyncAgentHook | None] = None
    async_on_session_compact: ClassVar[AsyncAgentHook | None] = None
    async_on_session_shutdown: ClassVar[AsyncAgentHook | None] = None
    async_on_session_before_tree: ClassVar[AsyncAgentHook | None] = None
    async_on_session_tree: ClassVar[AsyncAgentHook | None] = None
    async_on_context: ClassVar[AsyncAgentHook | None] = None
    async_on_before_provider_request: ClassVar[AsyncAgentHook | None] = None
    async_on_after_provider_response: ClassVar[AsyncAgentHook | None] = None
    async_on_before_agent_start: ClassVar[AsyncAgentHook | None] = None
    async_on_model_select: ClassVar[AsyncAgentHook | None] = None
    async_on_thinking_level_select: ClassVar[AsyncAgentHook | None] = None
    async_on_tool_call: ClassVar[AsyncAgentHook | None] = None
    async_on_tool_result: ClassVar[AsyncAgentHook | None] = None
    async_on_user_bash: ClassVar[AsyncAgentHook | None] = None
    async_on_input: ClassVar[AsyncAgentHook | None] = None

    # Async decision hooks (19; mirrors the sync decision block)
    async_decide_resources_discover: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_start: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_before_switch: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_before_fork: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_before_compact: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_compact: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_shutdown: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_before_tree: ClassVar[AsyncDecisionHook | None] = None
    async_decide_session_tree: ClassVar[AsyncDecisionHook | None] = None
    async_decide_context: ClassVar[AsyncDecisionHook | None] = None
    async_decide_before_provider_request: ClassVar[AsyncDecisionHook | None] = None
    async_decide_after_provider_response: ClassVar[AsyncDecisionHook | None] = None
    async_decide_before_agent_start: ClassVar[AsyncDecisionHook | None] = None
    async_decide_model_select: ClassVar[AsyncDecisionHook | None] = None
    async_decide_thinking_level_select: ClassVar[AsyncDecisionHook | None] = None
    async_decide_tool_call: ClassVar[AsyncDecisionHook | None] = None
    async_decide_tool_result: ClassVar[AsyncDecisionHook | None] = None
    async_decide_user_bash: ClassVar[AsyncDecisionHook | None] = None
    async_decide_input: ClassVar[AsyncDecisionHook | None] = None

    @classmethod
    def _validate_declared_hook_names(cls) -> None:
        for attr_name, value in cls.__dict__.items():
            if value is None:
                continue
            if attr_name.startswith("async_on_"):
                event_name = attr_name[len("async_on_") :]
                if event_name not in cls._OBSERVABLE_EVENT_NAMES:
                    raise TypeError(
                        f"{cls.__name__}.{attr_name} uses unsupported event suffix "
                        f"{event_name!r}; expected one of "
                        f"{sorted(cls._OBSERVABLE_EVENT_NAMES)!r}"
                    )
                continue
            if attr_name.startswith("on_"):
                event_name = attr_name[len("on_") :]
                if event_name not in cls._OBSERVABLE_EVENT_NAMES:
                    raise TypeError(
                        f"{cls.__name__}.{attr_name} uses unsupported event suffix "
                        f"{event_name!r}; expected one of "
                        f"{sorted(cls._OBSERVABLE_EVENT_NAMES)!r}"
                    )
                continue
            if attr_name.startswith("async_decide_"):
                event_name = attr_name[len("async_decide_") :]
                if event_name not in cls._DECISION_EVENT_NAMES:
                    raise TypeError(
                        f"{cls.__name__}.{attr_name} uses unsupported decision event suffix "
                        f"{event_name!r}; expected one of "
                        f"{sorted(cls._DECISION_EVENT_NAMES)!r}"
                    )
                continue
            if attr_name.startswith("decide_"):
                event_name = attr_name[len("decide_") :]
                if event_name not in cls._DECISION_EVENT_NAMES:
                    raise TypeError(
                        f"{cls.__name__}.{attr_name} uses unsupported decision event suffix "
                        f"{event_name!r}; expected one of "
                        f"{sorted(cls._DECISION_EVENT_NAMES)!r}"
                    )

    @classmethod
    def _validate_effective_hook_collisions(cls) -> None:
        for event_name in cls._OBSERVABLE_EVENT_NAMES:
            async_handler = getattr(cls, f"async_on_{event_name}", None)
            sync_handler = getattr(cls, f"on_{event_name}", None)
            if async_handler is not None and sync_handler is not None:
                raise TypeError(
                    f"{cls.__name__} defines both async_on_{event_name} and on_{event_name}; "
                    "clear one of them by assigning None"
                )
        for event_name in cls._DECISION_EVENT_NAMES:
            async_handler = getattr(cls, f"async_decide_{event_name}")
            sync_handler = getattr(cls, f"decide_{event_name}")
            if async_handler is not None and sync_handler is not None:
                raise TypeError(
                    f"{cls.__name__} defines both async_decide_{event_name} and "
                    f"decide_{event_name}; clear one of them by assigning None"
                )

    @classmethod
    def _validate_decision_timeouts(cls) -> None:
        if cls._decision_timeout_ms is not None and cls._decision_timeout_ms <= 0:
            raise TypeError(f"{cls.__name__}._decision_timeout_ms must be positive or None")
        for event_name, timeout_ms in cls._decision_timeouts_ms.items():
            if event_name not in cls._DECISION_EVENT_NAMES:
                raise TypeError(
                    f"{cls.__name__}._decision_timeouts_ms uses unsupported decision event "
                    f"{event_name!r}; expected one of {sorted(cls._DECISION_EVENT_NAMES)!r}"
                )
            if timeout_ms <= 0:
                raise TypeError(
                    f"{cls.__name__}._decision_timeouts_ms[{event_name!r}] must be positive"
                )


def _assert_declarations_match_event_sets(cls: type[AgentHookSurface]) -> None:
    """Pin the invariant that ``AgentHookSurface``'s hook ClassVars match the event sets.

    Runs on the base ``AgentHookSurface`` at import time. Future drift — adding a name
    to ``_EVENT_NAMES`` or ``_DECISION_EVENT_NAMES`` without adding the matching
    ClassVar declarations, or vice versa — becomes a load-time failure rather than a
    runtime mystery.
    """
    declared = {n for n in cls.__dict__ if n.startswith(("on_", "async_on_"))}
    declared_decision = {n for n in cls.__dict__ if n.startswith(("decide_", "async_decide_"))}

    expected_obs: set[str] = set()
    for event_name in cls._EVENT_NAMES | cls._DECISION_EVENT_NAMES:
        expected_obs.add(f"on_{event_name}")
        expected_obs.add(f"async_on_{event_name}")
    expected_decision: set[str] = set()
    for event_name in cls._DECISION_EVENT_NAMES:
        expected_decision.add(f"decide_{event_name}")
        expected_decision.add(f"async_decide_{event_name}")

    missing_obs = expected_obs - declared
    extra_obs = declared - expected_obs
    missing_decision = expected_decision - declared_decision
    extra_decision = declared_decision - expected_decision

    problems: list[str] = []
    if missing_obs:
        problems.append(f"missing observation ClassVars: {sorted(missing_obs)}")
    if extra_obs:
        problems.append(f"orphan observation ClassVars (no matching event): {sorted(extra_obs)}")
    if missing_decision:
        problems.append(f"missing decision ClassVars: {sorted(missing_decision)}")
    if extra_decision:
        problems.append(f"orphan decision ClassVars (no matching event): {sorted(extra_decision)}")
    if problems:
        raise AssertionError(
            "AgentHookSurface declarations diverged from the event sets:\n  - "
            + "\n  - ".join(problems)
        )


_assert_declarations_match_event_sets(AgentHookSurface)
