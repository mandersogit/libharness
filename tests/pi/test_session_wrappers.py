"""Wire-shape regression tests for the pi-native session method wrappers.

These tests pin the wire shape each typed wrapper on ``PiRpcClient``
serializes against pi's RPC contract
(``packages/coding-agent/src/modes/rpc/rpc-types.ts``). They follow the
same pattern as ``test_set_model.py``: the fake pi process echoes the
incoming request back under ``data.received`` so the test asserts on
exactly what the client put on the wire.

Pi's RPC contract is narrower than the original task description for
several of these wrappers — pi operates on the *current* session for
``fork`` / ``clone`` / ``get_session_stats`` / ``set_session_name`` /
``get_fork_messages`` (no session-id argument), addresses sessions by
*path* (not id) for ``switch_session``, and accepts an optional
``outputPath`` (not a session id) for ``export_html``. The wrappers
mirror pi's actual contract; these tests pin that contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from libharness.pi.rpc import PiLaunchConfig, PiRpcClient


def _fake_pi_config() -> PiLaunchConfig:
    script = Path(__file__).with_name("fake_pi_rpc.py")
    return PiLaunchConfig(
        pi_command=[sys.executable, str(script)],
        request_timeout=5,
        offline=False,
        no_extensions=False,
        no_skills=False,
        no_prompt_templates=False,
        no_context_files=False,
    )


def _received(response: dict) -> dict:
    return (response.get("data") or {}).get("received") or {}


async def test_fork_sends_entryId() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.fork("entry-42")

    received = _received(response)
    assert received.get("type") == "fork"
    assert received.get("entryId") == "entry-42"
    # Pi's contract does not accept any other fields; pin that we don't
    # send legacy/invented keys like `parentSession` or `sessionId`.
    assert "parentSession" not in received
    assert "sessionId" not in received


async def test_clone_takes_no_arguments() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.clone()

    received = _received(response)
    assert received.get("type") == "clone"
    # Only `type` and the framework-assigned `id` should be on the wire.
    assert set(received.keys()) == {"type", "id"}


async def test_switch_session_sends_sessionPath() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.switch_session("/tmp/sessions/abc.json")

    received = _received(response)
    assert received.get("type") == "switch_session"
    assert received.get("sessionPath") == "/tmp/sessions/abc.json"
    # Pi addresses sessions by path, not id — pin we don't send `sessionId`.
    assert "sessionId" not in received


async def test_get_session_stats_takes_no_arguments() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.get_session_stats()

    received = _received(response)
    assert received.get("type") == "get_session_stats"
    assert set(received.keys()) == {"type", "id"}


async def test_export_html_omits_outputPath_when_unset() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.export_html()

    received = _received(response)
    assert received.get("type") == "export_html"
    # Optional in pi's contract — omitting it must not send a null/empty key.
    assert "outputPath" not in received


async def test_export_html_sends_outputPath_when_provided() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.export_html("/tmp/out.html")

    received = _received(response)
    assert received.get("type") == "export_html"
    assert received.get("outputPath") == "/tmp/out.html"


async def test_set_session_name_sends_name() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.set_session_name("my session")

    received = _received(response)
    assert received.get("type") == "set_session_name"
    assert received.get("name") == "my session"


async def test_get_fork_messages_takes_no_arguments() -> None:
    async with PiRpcClient(_fake_pi_config()) as client:
        response = await client.get_fork_messages()

    received = _received(response)
    assert received.get("type") == "get_fork_messages"
    assert set(received.keys()) == {"type", "id"}


@pytest.mark.parametrize(
    "method_name,kwargs,expected_type",
    [
        ("fork", {"entry_id": "e1"}, "fork"),
        ("clone", {}, "clone"),
        ("switch_session", {"session_path": "/p"}, "switch_session"),
        ("get_session_stats", {}, "get_session_stats"),
        ("export_html", {}, "export_html"),
        ("set_session_name", {"name": "n"}, "set_session_name"),
        ("get_fork_messages", {}, "get_fork_messages"),
    ],
)
async def test_session_wrapper_type_field(
    method_name: str, kwargs: dict, expected_type: str
) -> None:
    """Each wrapper must serialize ``type`` exactly matching pi's RPC contract."""
    async with PiRpcClient(_fake_pi_config()) as client:
        method = getattr(client, method_name)
        response = await method(**kwargs)

    assert _received(response).get("type") == expected_type
