"""F14: reader task death surfaces fast on subsequent send().

The Pi RPC reader (`_read_stdout_loop`) can die in two distinct ways:

1. **Per-record JSONL decode error** — pi emits one malformed line. The
   decoder advances past it; the reader stays alive; the error is
   logged + written to stderr + counted on the client. *Reader does NOT
   die for this case post-F14.* Covered by `test_jsonl.py`.

2. **Catastrophic exception** (pipe broken, buffer overflow, asyncio
   framework error) — the reader task exits. Without F14, subsequent
   `send()` calls would write to pi's stdin, queue a Future in
   `_pending`, and time out `request_timeout` seconds later. With F14,
   `send()` checks `_reader_failure` and raises immediately with a
   diagnostic message.

This file covers case (2). Case (1) is exercised in `test_jsonl.py`
(decoder unit tests) plus is implicitly covered by the live-pi tests
(which would surface a regression if real pi output ever happened to
trip the per-record path).
"""

from __future__ import annotations

import pytest

from libharness.pi.rpc import PiLaunchConfig, PiRpcClient, PiRpcProcessError


@pytest.mark.asyncio
async def test_send_fails_fast_when_reader_died() -> None:
    """Simulate a reader-task crash; the next send() must raise
    `PiRpcProcessError` immediately, not queue and time out."""
    client = PiRpcClient(PiLaunchConfig(pi_command=["echo"]))
    # Simulate the post-crash state without actually starting pi.
    client.process = None  # send() also checks process; bypass for unit test
    client._reader_failure = RuntimeError("simulated reader crash")

    # We need a fake process for send() to even try writing. But the
    # _reader_failure check happens FIRST, before the process check, so
    # we should raise on _reader_failure without needing a process.
    with pytest.raises(PiRpcProcessError) as excinfo:
        await client.send({"type": "get_state"}, timeout=1.0)
    msg = str(excinfo.value)
    assert "reader task died" in msg
    assert "simulated reader crash" in msg
    # The original exception is chained for forensic debugging.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "simulated reader crash"


@pytest.mark.asyncio
async def test_send_works_normally_when_reader_alive() -> None:
    """Sanity: with `_reader_failure` unset, send() proceeds to the
    process check (and raises the not-started error there, since this
    unit test doesn't actually start pi)."""
    client = PiRpcClient(PiLaunchConfig(pi_command=["echo"]))
    assert client._reader_failure is None

    with pytest.raises(PiRpcProcessError) as excinfo:
        await client.send({"type": "get_state"}, timeout=1.0)
    # Should fall through the _reader_failure check to the not-started
    # error — i.e. the F14 guard didn't fire spuriously.
    assert "not started" in str(excinfo.value)


def test_decode_error_counters_start_at_zero() -> None:
    """The visibility counters on the client are zero before any reads."""
    client = PiRpcClient(PiLaunchConfig(pi_command=["echo"]))
    assert client._jsonl_decode_error_count == 0
    assert client._first_jsonl_decode_error is None
    assert client._last_jsonl_decode_error is None
    assert client._reader_failure is None


@pytest.mark.asyncio
async def test_decoder_errors_increment_counter_and_print_to_stderr(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive the reader loop with a chunk containing one good record and
    one malformed record. Assert the counter increments, the error is
    stashed on the client, the logger captures it at ERROR level, and
    the stderr fallback also fires."""
    import logging

    from libharness.pi.jsonl import dumps_line

    client = PiRpcClient(PiLaunchConfig(pi_command=["echo"]))

    # Build a fake process whose stdout returns a mixed chunk then EOF.
    chunks = [
        dumps_line({"type": "agent_start", "id": "1"}) + b"this is not json\n" + dumps_line(
            {"type": "agent_end", "id": "2"}
        ),
        b"",
    ]

    class _FakeReader:
        def __init__(self, parts: list[bytes]) -> None:
            self._parts = parts

        async def read(self, _n: int) -> bytes:
            return self._parts.pop(0) if self._parts else b""

    class _FakeProcess:
        def __init__(self, parts: list[bytes]) -> None:
            self.stdout = _FakeReader(parts)

    client.process = _FakeProcess(chunks)  # type: ignore[assignment]
    seen_events: list[dict] = []
    client.on_event(lambda e: seen_events.append(dict(e)))

    with caplog.at_level(logging.ERROR, logger="libharness.pi.rpc"):
        await client._read_stdout_loop()

    # Good records still delivered.
    assert [e["type"] for e in seen_events] == ["agent_start", "agent_end"]
    # Counter incremented; first/last error stashed.
    assert client._jsonl_decode_error_count == 1
    assert client._first_jsonl_decode_error is not None
    assert "this is not json" in client._first_jsonl_decode_error
    assert client._last_jsonl_decode_error == client._first_jsonl_decode_error
    # Logger fired.
    log_messages = [r.getMessage() for r in caplog.records]
    assert any("malformed JSONL from pi" in m for m in log_messages)
    # Stderr fallback fired too.
    captured = capsys.readouterr()
    assert "libharness.pi: malformed JSONL from pi" in captured.err
