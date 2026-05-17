"""Subclassable hook API layered on top of :class:`PiAgentHarness`.

The hook layer is intentionally additive. ``PiRpcClient.on_event(handler)`` and
``PiRpcClient.next_event()`` remain the lower-level subscription APIs; ``Agent``
installs one additional subscriber that dispatches selected Pi agent-loop events
to parallel ``async_on_*`` / ``on_*`` methods.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from .agent import PiAgentHarness


@dataclass(frozen=True, slots=True)
class AgentEvent(Mapping[str, Any]):
    """Frozen wrapper around Pi's raw event payload.

    The wrapper keeps the hook API small while providing convenient ``event.type``
    access. The original Pi fields remain available through ``event.payload`` and
    the mapping methods ``event["field"]`` / ``event.get("field")``.
    """

    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, event: Mapping[str, Any]) -> "AgentEvent":
        return cls(dict(event))

    @property
    def type(self) -> str:
        return str(self.payload.get("type") or "")

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


class UnhandledEventError(RuntimeError):
    """Raised by strict ``Agent`` subclasses for known events without hooks."""


AsyncAgentHook = Callable[[AgentEvent], Awaitable[object]]
SyncAgentHook = Callable[[AgentEvent], object]


class Agent(PiAgentHarness):
    """Subclassable synchronous Pi harness with named event hooks.

    Users normally subclass ``Agent`` and override either ``async_on_<event>`` or
    ``on_<event>`` for each event they care about. Asynchronous hooks run on the
    shared runtime asyncio loop thread. Synchronous hooks run on the runtime's
    dedicated single-worker hook executor; their return values are discarded.
    """

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
        }
    )
    _raise_on_unhandled_event: ClassVar[bool] = False

    async_on_agent_start: ClassVar[AsyncAgentHook | None] = None
    on_agent_start: ClassVar[SyncAgentHook | None] = None
    async_on_agent_end: ClassVar[AsyncAgentHook | None] = None
    on_agent_end: ClassVar[SyncAgentHook | None] = None
    async_on_turn_start: ClassVar[AsyncAgentHook | None] = None
    on_turn_start: ClassVar[SyncAgentHook | None] = None
    async_on_turn_end: ClassVar[AsyncAgentHook | None] = None
    on_turn_end: ClassVar[SyncAgentHook | None] = None
    async_on_message_start: ClassVar[AsyncAgentHook | None] = None
    on_message_start: ClassVar[SyncAgentHook | None] = None
    async_on_message_update: ClassVar[AsyncAgentHook | None] = None
    on_message_update: ClassVar[SyncAgentHook | None] = None
    async_on_message_end: ClassVar[AsyncAgentHook | None] = None
    on_message_end: ClassVar[SyncAgentHook | None] = None
    async_on_tool_execution_start: ClassVar[AsyncAgentHook | None] = None
    on_tool_execution_start: ClassVar[SyncAgentHook | None] = None
    async_on_tool_execution_update: ClassVar[AsyncAgentHook | None] = None
    on_tool_execution_update: ClassVar[SyncAgentHook | None] = None
    async_on_tool_execution_end: ClassVar[AsyncAgentHook | None] = None
    on_tool_execution_end: ClassVar[SyncAgentHook | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._validate_declared_hook_names()
        cls._validate_effective_hook_collisions()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._agent_event_unsubscribe: Callable[[], None] | None = None
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
        if name not in self._EVENT_NAMES:
            if self._raise_on_unhandled_event:
                raise UnhandledEventError(f"no named hook is declared for event type {name!r}")
            return

        async_handler = getattr(self, f"async_on_{name}")
        if async_handler is not None:
            result = async_handler(wrapped)
            if not inspect.isawaitable(result):
                raise TypeError(f"async_on_{name} must return an awaitable")
            await result
            return

        sync_handler = getattr(self, f"on_{name}")
        if sync_handler is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self.runtime.hook_executor, sync_handler, wrapped)
            return

        if self._raise_on_unhandled_event:
            raise UnhandledEventError(f"no handler defined for event of type {name!r}")

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
    def _validate_declared_hook_names(cls) -> None:
        prefixes = ("async_on_", "on_")
        for attr_name, value in cls.__dict__.items():
            if value is None:
                continue
            for prefix in prefixes:
                if attr_name.startswith(prefix):
                    event_name = attr_name[len(prefix) :]
                    if event_name not in cls._EVENT_NAMES:
                        raise TypeError(
                            f"{cls.__name__}.{attr_name} uses unsupported event suffix "
                            f"{event_name!r}; expected one of {sorted(cls._EVENT_NAMES)!r}"
                        )

    @classmethod
    def _validate_effective_hook_collisions(cls) -> None:
        for event_name in cls._EVENT_NAMES:
            async_handler = getattr(cls, f"async_on_{event_name}")
            sync_handler = getattr(cls, f"on_{event_name}")
            if async_handler is not None and sync_handler is not None:
                raise TypeError(
                    f"{cls.__name__} defines both async_on_{event_name} and on_{event_name}; "
                    "clear one of them by assigning None"
                )
