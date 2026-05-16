import json

import pytest

from pi_python_harness.jsonl import StrictJsonlBuffer, dumps_line


def test_strict_jsonl_splits_only_lf():
    parser = StrictJsonlBuffer()
    payload = {"text": "line separator \u2028 is data, not framing"}
    raw = dumps_line(payload)
    assert parser.feed(raw[:10]) == []
    assert parser.feed(raw[10:]) == [payload]


def test_strict_jsonl_accepts_crlf():
    parser = StrictJsonlBuffer()
    assert parser.feed(b'{"ok":true}\r\n') == [{"ok": True}]


def test_strict_jsonl_rejects_unterminated_record_on_close():
    parser = StrictJsonlBuffer()
    assert parser.feed(b'{"x":1}') == []
    with pytest.raises(ValueError):
        parser.close()
