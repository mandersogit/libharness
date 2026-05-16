from pi_python_harness.jsonl import JsonlDecoder, dumps_line, loads_lines


def test_jsonl_decoder_splits_lf_only():
    decoder = JsonlDecoder()
    payload = {"text": "contains unicode separator \u2028 but not a record boundary"}
    data = dumps_line(payload)
    # Feed in awkward chunks to exercise buffering.
    lines = []
    for idx in range(0, len(data), 7):
        lines.extend(decoder.feed(data[idx : idx + 7]))
    assert loads_lines(lines) == [payload]
