from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from pi_python_harness import Agent


CORE_AGENT_EVENTS = {
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
SESSION_LAYER_EVENTS = {
    "queue_update",
    "compaction_start",
    "compaction_end",
    "session_info_changed",
    "thinking_level_changed",
    "auto_retry_start",
    "auto_retry_end",
}


def _find_pi_source_root() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("PI_SOURCE_ROOT"):
        candidates.append(Path(os.environ["PI_SOURCE_ROOT"]))
    candidates.extend(
        [
            Path("links/pi"),
            Path("../pi"),
            Path("/mnt/data/pi_src/pi-main"),
        ]
    )
    for candidate in candidates:
        if (candidate / "packages/coding-agent/src/core/extensions/types.ts").exists():
            return candidate
    return None


def test_event_taxonomy_matches_pi_sources_when_available() -> None:
    root = _find_pi_source_root()
    if root is None:
        pytest.skip("Pi source tree not available")

    agent_types = (root / "packages/agent/src/types.ts").read_text(encoding="utf-8")
    session_types = (root / "packages/coding-agent/src/core/agent-session.ts").read_text(
        encoding="utf-8"
    )
    extension_types = (root / "packages/coding-agent/src/core/extensions/types.ts").read_text(
        encoding="utf-8"
    )

    agent_event_block = agent_types.split("export type AgentEvent =", 1)[1].split(
        "export interface", 1
    )[0]
    core_events = set(re.findall(r'type: "([a-z_]+)"', agent_event_block))
    assert core_events == CORE_AGENT_EVENTS

    session_event_block = session_types.split("export type AgentSessionEvent =", 1)[1].split(
        "/** Listener function", 1
    )[0]
    session_events = set(re.findall(r'type: "([a-z_]+)"', session_event_block))
    session_events.discard("AgentEvent")
    assert SESSION_LAYER_EVENTS.issubset(session_events)

    extension_on_events = set(re.findall(r'on\(\s*event: "([a-z_]+)"', extension_types))
    assert len(extension_on_events) == 29
    assert Agent._EVENT_NAMES == CORE_AGENT_EVENTS | SESSION_LAYER_EVENTS | {"extension_error"}
    assert Agent._DECISION_EVENT_NAMES == extension_on_events - CORE_AGENT_EVENTS
    assert len(Agent._EVENT_NAMES) == 18
    assert len(Agent._DECISION_EVENT_NAMES) == 19
