"""Owner-facing boundaries: real graph, selected exports, strict input, exit codes."""
import json
from pathlib import Path

import pytest

from sixeyes.cli import main
from sixeyes.ingest.jsonl import JsonlFormatError
from sixeyes.ingest.strict_jsonl import parse_strict_jsonl

SECRET = "SYNTHETIC-PRIVATE-CONTENT-987"


def request():
    return {"request_id": SECRET, "timestamp": 1, "model": SECRET,
            "system": SECRET, "messages": [{"role": "user", "content": SECRET}]}


@pytest.mark.parametrize("changed,fail,expected", [(False, False, 0), (True, False, 0), (True, True, 1)])
def test_cli_real_graph_exports_only_selected_fields(tmp_path, capsys, monkeypatch, changed, fail, expected):
    def no_persistent_key():
        pytest.fail("The CLI must not read or create a persistent fingerprint key")

    monkeypatch.setattr("sixeyes.fingerprint.fingerprint.load_or_create_key", no_persistent_key)
    first, second = request(), request()
    if changed:
        second["system"] += " changed"
    else:
        second["messages"].append({"role": "assistant", "content": SECRET})
    path = tmp_path / (SECRET + ".jsonl")
    text = "\ufeff" + "\r\n".join(json.dumps(r) for r in [first, second])
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    args = ["analyze", str(path), "--format", "json"] + (["--fail-on-change"] if fail else [])
    assert main(args) == expected
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert output.err == ""
    report = json.loads(output.out)
    assert report["request_count"] == 2
    assert report["changed_pair_count"] == int(changed)
    assert report["comparisons"][0]["change"] == ("system_changed" if changed else "none")
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("count", [0, 1])
def test_insufficient_requests_are_not_a_clean_bill_of_health(tmp_path, capsys, count):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(request()) if count else "", encoding="utf-8")
    assert main(["analyze", str(path)]) == 3
    assert "Insufficient requests" in capsys.readouterr().out


@pytest.mark.parametrize("field,value", [
    ("system", {"secret": SECRET}), ("messages", [{"role": SECRET}]),
    ("messages", [{"role": "user", "content": [SECRET]}]),
    ("timestamp", True), ("model", 123), (SECRET, SECRET),
    ("usage", {"input_tokens": -1}), ("tools", [{"name": "tool", SECRET: SECRET}]),
])
def test_unsupported_shapes_and_secrets_fail_closed(tmp_path, capsys, field, value):
    row = request()
    row[field] = value
    path = tmp_path / "input.jsonl"
    path.write_text("\n" + json.dumps(row), encoding="utf-8")
    assert main(["analyze", str(path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "line 2" in output.err
    assert SECRET not in output.err


@pytest.mark.parametrize("text", ['{"model":"a","model":"b"}', '{"timestamp":NaN}', '{"timestamp":1e999}'])
def test_duplicate_keys_and_nonfinite_numbers_are_rejected(text):
    with pytest.raises(JsonlFormatError):
        parse_strict_jsonl(text)


def test_missing_file_and_invalid_encoding_hide_paths_and_payload(tmp_path, capsys):
    path = tmp_path / SECRET
    assert main(["analyze", str(path)]) == 2
    path.write_bytes(b"\xff" + SECRET.encode())
    assert main(["analyze", str(path)]) == 2
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "Traceback" not in output.err


def test_limits_fail_without_partial_report(tmp_path, capsys, monkeypatch):
    path = tmp_path / "input.jsonl"
    path.write_text(json.dumps(request()), encoding="utf-8")
    monkeypatch.setattr("sixeyes.cli.MAX_INPUT_BYTES", 4)
    assert main(["analyze", str(path)]) == 2
    assert capsys.readouterr().out == ""
    with pytest.raises(JsonlFormatError):
        parse_strict_jsonl("\n".join([json.dumps(request())] * 2), max_requests=1)


def test_raw_tool_arguments_are_preserved_and_nested_extras_rejected():
    row = request()
    args = '{ invalid model output ' + SECRET
    row["messages"] = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call", "name": "search", "arguments": args},
    ]}]
    trace = parse_strict_jsonl(json.dumps(row))
    assert trace.requests[0].messages[0].tool_calls[0].arguments_raw == args
    row["messages"][0]["tool_calls"][0][SECRET] = SECRET
    with pytest.raises(JsonlFormatError) as caught:
        parse_strict_jsonl(json.dumps(row))
    assert SECRET not in str(caught.value)
