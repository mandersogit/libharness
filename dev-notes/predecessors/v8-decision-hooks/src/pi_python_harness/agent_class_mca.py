"""Subclassable hook API layered on top of :class:`PiAgentHarness`.

The hook layer is intentionally additive. ``PiRpcClient.on_event(handler)`` and
``PiRpcClient.next_event()`` remain the lower-level RPC stdout subscription APIs;
``Agent`` installs one additional subscriber for named RPC notification events and
also services decision events that arrive over the TypeScript bridge channel.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar, cast

from .agent import PiAgentHarness

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentEvent(Mapping[str, Any]):
    """Immutable wrapper around Pi's raw event payload.

    The wrapper keeps the hook API small while providing convenient ``event.type``
    access. The original Pi fields remain available through mapping methods such
    as ``event["field"]`` and ``event.get("field")``. ``event.payload`` is a
    shallow immutable mapping; nested values are preserved as Pi supplied them.
    """

    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, event: Mapping[str, Any]) -> "AgentEvent":
        return cls(MappingProxyType(dict(event)))

    @property
    def type(self) -> str:
        return str(self.payload.get("type") or "")

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class HookContext:
    """Cooperative cancellation context for decision hooks.

    Decision hooks may block Pi's in-process event handler. If the bridge
    connection closes while Python is deciding, ``cancelled`` flips to ``True``.
    Long-running hooks should poll it and return promptly, usually returning
    ``None`` to express no opinion.
    """

    event_name: str
    request_id: str | None = None
    _cancelled: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled.is_set())


class UnhandledEventError(RuntimeError):
    """Raised by strict ``Agent`` subclasses for known events without hooks."""


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


class Agent(AgentHookSurface, PiAgentHarness):
    """Subclassable synchronous Pi harness with notification and decision hooks.

    Users normally subclass ``Agent`` and override either ``async_on_<event>`` or
    ``on_<event>`` for events they want to observe. Asynchronous notification hooks
    run on the shared runtime asyncio loop thread. Synchronous notification hooks
    run on the runtime's dedicated single-worker hook executor; their return values
    are discarded.

    For Pi's in-process extension event surface, subclasses override exactly one
    of ``async_decide_<event>`` or ``decide_<event>``. The method's existence opens
    the corresponding bridge gate at extension load time. Decision hook return
    values are raw Python values that must match Pi's TypeScript event result
    shape; ``None`` means no opinion.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._validate_declared_hook_names()
        cls._validate_effective_hook_collisions()
        cls._validate_decision_timeouts()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._agent_event_unsubscribe: Callable[[], None] | None = None
        kwargs["bridge_event_handler"] = self._async_on_bridge_event
        kwargs["initial_open_gates"] = tuple(self._initial_open_gates_for_class())
        kwargs["decision_timeouts_ms"] = self._decision_timeouts_for_manifest()
        super().__init__(*args, **kwargs)

    def start(self) -> "Agent":
        super().start()
        self._install_agent_event_dispatcher()
        return self

    def close(self) -> None:
        try:
            self._uninstall_agent_event_dispatcher()
        finally:
            super().close()

    async def _async_on_event(self, event: AgentEvent | Mapping[str, Any]) -> None:
        wrapped = event if isinstance(event, AgentEvent) else AgentEvent.from_mapping(event)
        name = wrapped.type
        if name in self._RPC_REQUEST_TYPES:
            return
        if name not in self._EVENT_NAMES:
            if self._raise_on_unhandled_event:
                raise UnhandledEventError(f"no named hook is declared for event type {name!r}")
            return
        await self._dispatch_notification_hook(wrapped, strict=True)

    async def _async_on_bridge_event(
        self,
        event: Mapping[str, Any],
        cancelled: threading.Event,
        require_decision: bool,
        request_id: str | None,
    ) -> object | None:
        wrapped = AgentEvent.from_mapping(event)
        if wrapped.type not in self._DECISION_EVENT_NAMES:
            if self._raise_on_unhandled_event:
                raise UnhandledEventError(
                    f"no decision hook is declared for event type {wrapped.type!r}"
                )
            return None

        await self._dispatch_notification_hook(wrapped, strict=False)
        if not require_decision:
            return None
        ctx = HookContext(wrapped.type, request_id=request_id, _cancelled=cancelled)
        return await self._dispatch_decision_hook(wrapped, ctx)

    async def _dispatch_notification_hook(self, event: AgentEvent, *, strict: bool) -> None:
        name = event.type
        async_handler = getattr(self, f"async_on_{name}", None)
        sync_handler = getattr(self, f"on_{name}", None)

        if async_handler is not None:
            try:
                result = async_handler(event)
                if not inspect.isawaitable(result):
                    raise TypeError(f"async_on_{name} must return an awaitable")
                await result
            except Exception:
                _LOG.exception("Agent notification hook failed for event %s", name)
            return

        if sync_handler is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self.runtime.hook_executor, sync_handler, event)
            except Exception:
                _LOG.exception("Agent notification hook failed for event %s", name)
            return

        if strict and self._raise_on_unhandled_event:
            raise UnhandledEventError(f"no handler defined for event of type {name!r}")

    async def _dispatch_decision_hook(self, event: AgentEvent, ctx: HookContext) -> object | None:
        name = event.type
        async_handler = getattr(self, f"async_decide_{name}", None)
        sync_handler = getattr(self, f"decide_{name}", None)

        if async_handler is not None:
            try:
                result = _call_decision_handler(async_handler, event, ctx)
                if not inspect.isawaitable(result):
                    raise TypeError(f"async_decide_{name} must return an awaitable")
                awaitable = cast(Awaitable[object | None], result)
                return await awaitable
            except Exception:
                _LOG.exception("Agent decision hook failed for event %s", name)
                raise

        if sync_handler is not None:
            loop = asyncio.get_running_loop()

            def call() -> object | None:
                value = _call_decision_handler(sync_handler, event, ctx)
                if inspect.isawaitable(value):
                    raise TypeError(f"decide_{name} must not return an awaitable")
                return value

            try:
                return await loop.run_in_executor(self.runtime.hook_executor, call)
            except Exception:
                _LOG.exception("Agent decision hook failed for event %s", name)
                raise

        if self._raise_on_unhandled_event:
            raise UnhandledEventError(f"no decision handler defined for event of type {name!r}")
        return None

    def _install_agent_event_dispatcher(self) -> None:
        if self._agent_event_unsubscribe is not None:
            return
        self._agent_event_unsubscribe = self._call(
            lambda core: core.subscribe_client_events(self._async_on_event)
        )

    def _uninstall_agent_event_dispatcher(self) -> None:
        unsubscribe = self._agent_event_unsubscribe
        if unsubscribe is None:
            return
        self._agent_event_unsubscribe = None
        try:
            self._call(lambda core: core.unsubscribe_client_events(unsubscribe))
        except RuntimeError:
            unsubscribe()

    @classmethod
    def _initial_open_gates_for_class(cls) -> frozenset[str]:
        open_gates: set[str] = set()
        for event_name in cls._DECISION_EVENT_NAMES:
            if (
                getattr(cls, f"async_decide_{event_name}", None) is not None
                or getattr(cls, f"decide_{event_name}", None) is not None
            ):
                open_gates.add(event_name)
        return frozenset(open_gates)

    @classmethod
    def _decision_timeouts_for_manifest(cls) -> dict[str, int]:
        result: dict[str, int] = {}
        if cls._decision_timeout_ms is not None:
            for event_name in cls._initial_open_gates_for_class():
                result[event_name] = cls._decision_timeout_ms
        result.update(dict(cls._decision_timeouts_ms))
        return result

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


def _call_decision_handler(
    handler: Callable[..., object | Awaitable[object | None] | None],
    event: AgentEvent,
    ctx: HookContext,
) -> object | Awaitable[object | None] | None:
    if _handler_wants_context(handler):
        return handler(event, ctx)
    return handler(event)


def _handler_wants_context(handler: Callable[..., object]) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return True
    positional_count = 0
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            positional_count += 1
        if param.kind is inspect.Parameter.KEYWORD_ONLY and param.name in {"ctx", "context"}:
            return True
    return positional_count >= 2


# Keep pyright from narrowing the class attributes to their base-class literal values only.
cast(object, Agent)
