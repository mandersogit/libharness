"""Public data types for the ``libharness.pi`` hook API.

These three types are the public surface that hook authors interact with:

* :class:`AgentEvent` wraps Pi's raw event payload in an immutable mapping
  with a convenient ``event.type`` property.
* :class:`HookContext` is the cooperative-cancellation handle passed to
  decision hooks that opt in via a second positional parameter.
* :class:`UnhandledEventError` is raised by strict ``Agent`` subclasses
  for known events without an explicit hook.

The split into ``events.py`` (data types) / ``hook_surface.py`` (mixin) /
``agent_class.py`` (Agent class) follows the layout decision in
``dev-notes/2026-05-17-v8-analysis.md`` § File layout for the port.
Imports flow one direction: ``agent_class`` → ``hook_surface`` → ``events``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


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
    def from_mapping(cls, event: Mapping[str, Any]) -> AgentEvent:
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
