import pytest

from libharness.pi.jsonl import JsonlDecodeError, StrictJsonlDecoder, dumps_line


def test_jsonl_decoder_splits_only_on_lf() -> None:
    payload = {"text": "alpha beta gamma"}
    data = dumps_line(payload)
    decoder = StrictJsonlDecoder()
    assert decoder.feed(data[:5]) == ([], [])
    assert decoder.feed(data[5:]) == ([payload], [])


def test_jsonl_decoder_handles_multiple_frames() -> None:
    decoder = StrictJsonlDecoder()
    records, errors = decoder.feed(dumps_line({"a": 1}) + dumps_line({"b": 2}))
    assert records == [{"a": 1}, {"b": 2}]
    assert errors == []


def test_jsonl_decoder_skips_bad_records_and_continues() -> None:
    """F14: a single malformed record must not strand subsequent good records.
    The decoder collects the error in the returned errors list; the caller
    is responsible for surfacing it (logging, stderr) so a pi-side bug
    doesn't get silently swallowed."""
    decoder = StrictJsonlDecoder()
    good1 = dumps_line({"a": 1})
    bad = b"this is not json\n"
    good2 = dumps_line({"b": 2})
    records, errors = decoder.feed(good1 + bad + good2)
    assert records == [{"a": 1}, {"b": 2}]
    assert len(errors) == 1
    assert isinstance(errors[0], JsonlDecodeError)
    assert "this is not json" in str(errors[0])


def test_jsonl_decoder_recovers_after_bad_record_across_calls() -> None:
    """The decoder's state advances past the bad record so a subsequent
    feed() call continues normally."""
    decoder = StrictJsonlDecoder()
    records1, errors1 = decoder.feed(b"not json\n")
    assert records1 == []
    assert len(errors1) == 1
    records2, errors2 = decoder.feed(dumps_line({"x": 1}))
    assert records2 == [{"x": 1}]
    assert errors2 == []


def test_jsonl_decoder_buffer_overflow_still_raises() -> None:
    """Buffer-overflow is catastrophic (decoder is wedged) and still raises."""
    decoder = StrictJsonlDecoder(max_buffer_bytes=128)
    with pytest.raises(JsonlDecodeError, match="exceeded 128 bytes"):
        decoder.feed(b"x" * 256)
