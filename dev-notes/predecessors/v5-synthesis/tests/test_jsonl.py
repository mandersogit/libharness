from pi_python_harness.jsonl import StrictJsonlDecoder, dumps_line


def test_jsonl_decoder_splits_only_on_lf() -> None:
    payload = {"text": "alpha\u2028beta\u2029gamma"}
    data = dumps_line(payload)
    decoder = StrictJsonlDecoder()
    assert decoder.feed(data[:5]) == []
    assert decoder.feed(data[5:]) == [payload]


def test_jsonl_decoder_handles_multiple_frames() -> None:
    decoder = StrictJsonlDecoder()
    assert decoder.feed(dumps_line({"a": 1}) + dumps_line({"b": 2})) == [{"a": 1}, {"b": 2}]
