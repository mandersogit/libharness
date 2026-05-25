"""Strict-ish JSON Lines helpers for Pi RPC and the local tool bridge.

Pi's RPC docs specify LF-delimited JSON on stdin/stdout. The decoder below
splits only on byte LF, so Unicode line/paragraph separators embedded inside
JSON strings are not treated as transport delimiters.
"""

from __future__ import annotations

import json
from typing import Any


class JsonlDecodeError(ValueError):
    """Raised when a JSONL frame cannot be decoded."""


def dumps_line(value: Any) -> bytes:
    """Serialize *value* as one UTF-8 JSONL frame."""

    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class StrictJsonlDecoder:
    """Incremental byte-oriented JSONL decoder.

    The decoder intentionally looks for the byte value ``b"\\n"``. It does not
    use ``str.splitlines()``, which would incorrectly split on valid Unicode
    characters such as U+2028 embedded inside JSON strings.

    Per-record decode failures (malformed JSON for one record) are collected
    into the returned errors list rather than raised — the decoder's state
    advances past the bad record so subsequent records can still be
    delivered. This matters for ``rpc.py:_read_stdout_loop``: a transient
    pi-side bug emitting one malformed line should not kill the reader and
    take down the whole session. See F14 in
    ``dev-notes/2026-05-17-v8-port-deferred-items.md``.

    Buffer overflow (no LF seen within ``max_buffer_bytes``) still raises —
    that's catastrophic; the decoder's buffer is wedged and there's no
    sensible recovery.
    """

    def __init__(self, *, max_buffer_bytes: int = 16 * 1024 * 1024) -> None:
        self._buffer = bytearray()
        self._max_buffer_bytes = max_buffer_bytes

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> tuple[list[Any], list[JsonlDecodeError]]:
        """Feed bytes; return ``(records, errors)``.

        ``records`` are the successfully decoded JSON values, in arrival
        order. ``errors`` are the per-record decode failures encountered
        in this call (typically zero). Callers should log every entry in
        ``errors`` — silent skip would hide pi-side bugs.

        Raises ``JsonlDecodeError`` only on buffer overflow.
        """
        records: list[Any] = []
        errors: list[JsonlDecodeError] = []
        if not chunk:
            # Still process any complete records left in the buffer from
            # prior calls; empty chunk doesn't itself produce records.
            pass
        else:
            self._buffer.extend(chunk)
            if len(self._buffer) > self._max_buffer_bytes:
                raise JsonlDecodeError(
                    f"JSONL buffer exceeded {self._max_buffer_bytes} bytes without LF terminator"
                )

        while True:
            try:
                index = self._buffer.index(0x0A)  # LF only
            except ValueError:
                break

            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            try:
                records.append(json.loads(raw.decode("utf-8")))
            except Exception as exc:
                preview = raw[:200].decode("utf-8", "replace")
                errors.append(
                    JsonlDecodeError(
                        f"invalid JSONL record: {exc}; record starts with {preview!r}"
                    )
                )
        return records, errors

    def flush(self) -> None:
        if self._buffer:
            preview = bytes(self._buffer[:200]).decode("utf-8", "replace")
            raise JsonlDecodeError(f"unterminated JSONL record at EOF: {preview!r}")
