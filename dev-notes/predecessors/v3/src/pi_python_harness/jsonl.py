"""Strict LF-delimited JSONL utilities.

Pi RPC mode is explicitly LF-delimited. This module does not treat Unicode line
separator characters as record delimiters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def dumps_line(obj: Any) -> bytes:
    """Serialize one JSON object as UTF-8 bytes plus a single LF delimiter."""

    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass
class StrictJsonlBuffer:
    """Incrementally parse LF-delimited JSON records from bytes.

    Records are split only on byte ``0x0A``. A trailing carriage return is
    stripped to tolerate CRLF senders. Blank records are ignored.
    """

    _buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Any]:
        self._buffer.extend(data)
        records: list[Any] = []

        while True:
            try:
                idx = self._buffer.index(0x0A)
            except ValueError:
                break

            raw = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            records.append(json.loads(raw.decode("utf-8")))

        return records

    def close(self) -> None:
        """Raise if the stream ended with an unterminated record."""

        if self._buffer.strip():
            raise ValueError("unterminated JSONL record at end of stream")
        self._buffer.clear()
