"""The :class:`Agent` class — dispatch logic and lifecycle on top of the hook surface.

The declarative hook surface lives in :mod:`libharness.pi.hook_surface`; the
public data types live in :mod:`libharness.pi.events`. This file holds the
behavior: ``__init_subclass__`` validation triggers, ``__init__`` /
``start`` / ``close`` lifecycle, the two async dispatchers
(``_async_on_event`` for RPC notification, ``_async_on_bridge_event`` for
decision events), the install / uninstall helpers for the client-event
subscription, and the manifest-wiring classmethods that read the mixin to
shape ``PiAgentHarness`` constructor kwargs.

``PiRpcClient.on_event(handler)`` and ``PiRpcClient.next_event()`` remain
the lower-level RPC stdout subscription APIs; ``Agent`` installs one
additional subscriber for named RPC notification events and also services
decision events that arrive over the TypeScript bridge channel.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from .agent import PiAgentHarness
from .events import AgentEvent, HookContext, UnhandledEventError
from .hook_surface import AgentHookSurface

_LOG = logging.getLogger(__name__)


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

    def start(self) -> Agent:
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
            except Exception as exc:
                _LOG.exception("Agent decision hook failed for event %s", name)
                # Set a sentinel so the outer bridge layer doesn't log this
                # exception again. See `server.py`:_dispatch_bridge_event.
                exc._libharness_logged = True  # type: ignore[attr-defined]
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
            except Exception as exc:
                _LOG.exception("Agent decision hook failed for event %s", name)
                exc._libharness_logged = True  # type: ignore[attr-defined]
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
#
# Without this line, pyright performs literal-value narrowing on the 112 ClassVar
# declarations in `AgentHookSurface` (all of which default to `None`). User
# subclasses that assign a callable to e.g. `on_agent_start` then trigger
# spurious `Cannot assign to attribute of type None` errors because pyright
# believes the attribute's type is `None`, not `SyncAgentHook | None`. Casting
# the class to `object` forces pyright to drop the narrowing and respect the
# declared `ClassVar[SyncAgentHook | None]` type. Mypy doesn't need this; only
# pyright. Tracked as F28 in the deferred-items doc (now just documented, not
# deferred).
cast(object, Agent)
