from __future__ import annotations

import json
from typing import Any, Iterable


def dumps_line(value: Any) -> bytes:
    """Serialize a strict JSONL record using LF as the only delimiter."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class JsonlDecoder:
    """Incremental LF-only JSONL decoder.

    This mirrors Pi's own constraint: split on byte LF only. Unicode separators
    such as U+2028/U+2029 remain part of JSON strings.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> list[str]:
        self._buffer += chunk
        lines: list[str] = []
        while True:
            idx = self._buffer.find(b"\n")
            if idx < 0:
                break
            raw = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            lines.append(raw.decode("utf-8"))
        return lines

    def flush(self) -> list[str]:
        if not self._buffer:
            return []
        raw = self._buffer
        self._buffer = b""
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        return [raw.decode("utf-8")]


def loads_lines(lines: Iterable[str]) -> list[Any]:
    return [json.loads(line) for line in lines if line]
