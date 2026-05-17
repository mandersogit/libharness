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

    The decoder intentionally looks for the byte value ``b"\n"``. It does not
    use ``str.splitlines()``, which would incorrectly split on valid Unicode
    characters such as U+2028 embedded inside JSON strings.
    """

    def __init__(self, *, max_buffer_bytes: int = 16 * 1024 * 1024) -> None:
        self._buffer = bytearray()
        self._max_buffer_bytes = max_buffer_bytes

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Any]:
        if not chunk:
            return []
        self._buffer.extend(chunk)
        if len(self._buffer) > self._max_buffer_bytes:
            raise JsonlDecodeError(
                f"JSONL buffer exceeded {self._max_buffer_bytes} bytes without LF terminator"
            )

        records: list[Any] = []
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
            except (
                Exception
            ) as exc:  # pragma: no cover - exception type varies across Python versions
                preview = raw[:200].decode("utf-8", "replace")
                raise JsonlDecodeError(
                    f"invalid JSONL record: {exc}; record starts with {preview!r}"
                ) from exc
        return records

    def flush(self) -> None:
        if self._buffer:
            preview = bytes(self._buffer[:200]).decode("utf-8", "replace")
            raise JsonlDecodeError(f"unterminated JSONL record at EOF: {preview!r}")
