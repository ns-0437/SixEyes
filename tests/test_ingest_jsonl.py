from __future__ import annotations

import pytest

from sixeyes.ingest.jsonl import JsonlFormatError, canonical_json, parse_jsonl, parse_line
from sixeyes.ingest.types import RawMessage


def _line(**overrides: object) -> str:
    import json

    obj = {
        "request_id": "req_1",
        "timestamp": 1758000000.0,
        "model": "claude-opus-5",
        "system": "You are a helpful assistant.",
        "tools": [{"name": "search", "description": "web search", "schema": {"b": 1, "a": 2}}],
        "messages": [{"role": "user", "content": "hello"}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_parses_a_well_formed_line() -> None:
    req = parse_line(_line(), line_no=1)
    assert req.request_id == "req_1"
    assert req.model == "claude-opus-5"
    assert req.system == "You are a helpful assistant."
    assert len(req.tools) == 1
    assert req.tools[0].name == "search"
    assert req.messages == (RawMessage("user", "hello", None),)
    assert req.usage_input_tokens == 10
    assert req.usage_cache_read_tokens == 0


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_tool_schema_is_canonicalized_regardless_of_key_order() -> None:
    a = parse_line(_line(tools=[{"name": "t", "description": "d", "schema": {"x": 1, "y": 2}}]), 1)
    b = parse_line(_line(tools=[{"name": "t", "description": "d", "schema": {"y": 2, "x": 1}}]), 1)
    assert a.tools[0].schema_json == b.tools[0].schema_json


def test_missing_required_field_reports_line_number() -> None:
    import json

    obj = json.loads(_line())
    del obj["model"]
    with pytest.raises(JsonlFormatError) as excinfo:
        parse_line(json.dumps(obj), line_no=42)
    assert excinfo.value.line_no == 42
    assert "model" in str(excinfo.value)


def test_invalid_json_reports_line_number() -> None:
    with pytest.raises(JsonlFormatError) as excinfo:
        parse_line("{not json", line_no=7)
    assert excinfo.value.line_no == 7


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(JsonlFormatError):
        parse_line(_line(messages=[{"role": "narrator", "content": "hi"}]), 1)


def test_parse_jsonl_skips_blank_lines_and_preserves_order() -> None:
    text = "\n".join([_line(request_id="a"), "", "  ", _line(request_id="b")])
    trace = parse_jsonl(text, workload_id="wl_1")
    assert trace.workload_id == "wl_1"
    assert [r.request_id for r in trace.requests] == ["a", "b"]


def test_defaults_are_applied_for_optional_fields() -> None:
    import json

    minimal = {
        "request_id": "r",
        "timestamp": 1.0,
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
    }
    req = parse_line(json.dumps(minimal), 1)
    assert req.system is None
    assert req.tools == ()
    assert req.usage_input_tokens is None


def test_malformed_timestamp_error_does_not_echo_the_raw_value() -> None:
    """A malformed field can itself carry customer content (a secret pasted into the
    wrong field) -- float()'s own ValueError embeds the raw value verbatim, so it must be
    caught and replaced with a sanitised message before it ever reaches a caller, rather
    than relying on a downstream redaction layer to catch it after the fact."""
    import json

    marker = "SYNTHETIC-SECRET-IN-BAD-TIMESTAMP"
    obj = {"request_id": "r", "timestamp": marker, "model": "m", "messages": []}

    with pytest.raises(JsonlFormatError) as excinfo:
        parse_line(json.dumps(obj), line_no=3)

    assert marker not in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # `from None` -- no chained exception carries it either
    assert "timestamp" in str(excinfo.value)
    assert excinfo.value.line_no == 3


def test_utf8_bom_at_the_start_of_the_file_is_accepted() -> None:
    """Windows tools write a BOM; the file is otherwise valid JSONL."""
    bom, nl = chr(0xFEFF), chr(10)
    trace = parse_jsonl(bom + _line() + nl + _line(request_id="req_2"), workload_id="w")
    assert [r.request_id for r in trace.requests] == ["req_1", "req_2"]


def test_line_separator_characters_inside_content_do_not_split_a_line() -> None:
    """U+2028, U+2029 and U+0085 are legal unescaped in a JSON string and were treated as line
    breaks by str.splitlines(), so a valid export failed with 'Unterminated string'."""
    import json

    content = "a" + chr(0x2028) + "b" + chr(0x2029) + "c" + chr(0x85) + "d"
    line = json.dumps(
        {
            "request_id": "req_1",
            "timestamp": 1.0,
            "model": "m",
            "messages": [{"role": "user", "content": content}],
        },
        ensure_ascii=False,
    )
    trace = parse_jsonl(line + chr(10) + _line(request_id="req_2"), workload_id="w")
    assert [r.request_id for r in trace.requests] == ["req_1", "req_2"]
    assert trace.requests[0].messages[0].content == content


def test_crlf_line_endings_and_line_numbers_still_work() -> None:
    crlf = chr(13) + chr(10)
    bad = "not json"
    text = _line() + crlf + crlf + bad
    with pytest.raises(JsonlFormatError) as excinfo:
        parse_jsonl(text, workload_id="w")
    assert excinfo.value.line_no == 3


def test_null_tool_calls_and_tools_are_treated_as_absent() -> None:
    """OpenAI-style logs write `"tool_calls": null` on ordinary assistant turns; that used to
    raise TypeError from iterating None."""
    req = parse_line(
        _line(
            tools=None,
            messages=[{"role": "assistant", "content": "hi", "tool_calls": None}],
        ),
        line_no=1,
    )
    assert req.tools == ()
    assert req.messages[0].tool_calls == ()


@pytest.mark.parametrize(
    "overrides, field",
    [
        ({"messages": None}, "messages"),
        ({"messages": "hi"}, "messages"),
        ({"tools": {"name": "x"}}, "tools"),
        ({"messages": [{"role": "assistant", "tool_calls": "call_1"}]}, "tool_calls"),
    ],
)
def test_non_list_fields_are_a_format_error_with_a_line_number(
    overrides: dict[str, object], field: str
) -> None:
    with pytest.raises(JsonlFormatError) as excinfo:
        parse_line(_line(**overrides), line_no=7)
    assert excinfo.value.line_no == 7
    assert field in str(excinfo.value)
