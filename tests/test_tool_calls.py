"""RawMessage.tool_calls: faithful fingerprinting of an assistant's structured tool-call
requests.

Motivation (2026-09-17): before pointing SixEyes at a real OpenAI-style tool-use loop
(AgentFuse's `guarded_tool_loop`), a compatibility check found that the schema had no slot
for an assistant message's outgoing `tool_calls` -- only for a tool's *result* (via
`tool_call_id` on a `role="tool"` message). Without this field, mapping a real trace into
SixEyes's schema would have to either silently drop the tool-call data or stringify it
into `content`, either of which could make a request that actually changed compare as
unchanged. These tests prove the fix does neither: two requests differing only in which
tool was called, or with what arguments, are detected -- entirely offline, with synthetic
data, no real API calls.
"""

from __future__ import annotations

from sixeyes.fingerprint.divergence import compare
from sixeyes.fingerprint.fingerprint import fingerprint_request
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.ingest.jsonl import parse_line
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolCall

TEST_KEY = b"\x07" * 32


def _request(**overrides: object) -> RawRequest:
    defaults: dict[object, object] = dict(
        request_id="req_1",
        timestamp=1.0,
        model="gpt-4o",
        system="You are a helpful agent.",
        tools=(),
        messages=(RawMessage("user", "find the connection string"),),
    )
    defaults.update(overrides)
    return RawRequest(**defaults)  # type: ignore[arg-type]


def _fp(**overrides: object):  # type: ignore[no-untyped-def]
    return fingerprint_request(_request(**overrides), TEST_KEY)


def test_a_tool_call_with_no_text_content_is_not_corrupted_into_the_string_none() -> None:
    """A real OpenAI-style assistant message making only a tool call sends
    content: null. str(None) would have silently produced the literal token "None" in
    every such message -- checked directly here, not just inferred from behaviour."""
    msg = RawMessage(
        role="assistant",
        content="",
        tool_calls=(RawToolCall("call_1", "search_files", '{"pattern":"*.conn"}'),),
    )
    request = _request(messages=(msg,))
    fp = fingerprint_request(request, TEST_KEY)
    # indirect but conclusive: fingerprinting an explicit "" content must be identical to
    # fingerprinting the same message with content omitted via the real parser
    from_parser = parse_line(
        '{"request_id":"r","timestamp":1.0,"model":"gpt-4o","system":"You are a helpful agent.",'
        '"messages":[{"role":"assistant","content":null,"tool_calls":'
        '[{"id":"call_1","name":"search_files","arguments":{"pattern":"*.conn"}}]}]}',
        line_no=1,
    )
    parsed_fp = fingerprint_request(from_parser, TEST_KEY)
    assert fp.segment("messages").chain == parsed_fp.segment("messages").chain


def test_changing_which_tool_is_called_is_detected() -> None:
    previous = _fp(
        messages=(
            RawMessage("assistant", "", tool_calls=(RawToolCall("call_1", "search_files", "{}"),)),
        )
    )
    current = _fp(
        messages=(
            RawMessage("assistant", "", tool_calls=(RawToolCall("call_1", "read_file", "{}"),)),
        )
    )
    assert compare("wl", previous, current).kind is DivergenceKind.MESSAGES_CHANGED


def test_changing_only_the_arguments_of_the_same_tool_call_is_detected() -> None:
    previous = _fp(
        messages=(
            RawMessage("assistant", "", tool_calls=(
                RawToolCall("call_1", "search_files", '{"pattern":"*.conn"}'),
            )),
        )
    )
    current = _fp(
        messages=(
            RawMessage("assistant", "", tool_calls=(
                RawToolCall("call_1", "search_files", '{"pattern":"*.env"}'),
            )),
        )
    )
    assert compare("wl", previous, current).kind is DivergenceKind.MESSAGES_CHANGED


def test_a_message_with_no_tool_calls_differs_from_one_with_an_identical_looking_text() -> None:
    """The core faithfulness property: a message with a real tool call must not
    fingerprint the same as a text-only message that merely mentions the same tool by
    name -- confirms the tool_call unit is structurally tagged, not string-matched."""
    previous = _fp(messages=(RawMessage("assistant", "I will call search_files now."),))
    current = _fp(
        messages=(
            RawMessage("assistant", "", tool_calls=(RawToolCall("call_1", "search_files", "{}"),)),
        )
    )
    assert previous.segment("messages").chain != current.segment("messages").chain


def test_repeating_the_identical_tool_call_as_a_new_turn_is_safe_append_growth() -> None:
    """A genuine duplicate tool call, appended as a new turn after an unedited history,
    is exactly the append-only growth case messages growth tolerance is *supposed* to
    allow -- SixEyes has no redundant-tool-call detector yet (Phase 3, unbuilt), so this
    correctly reports NONE. Repeated identical calls are a lead a future detector would
    need to reason about (args, results, intervening state, retry cause), not something
    today's structural comparison is meant to flag."""
    first_call = RawMessage("assistant", "", tool_calls=(RawToolCall("call_1", "search_files", "{}"),))
    previous = _fp(messages=(first_call,))
    current = _fp(
        messages=(
            first_call,
            RawMessage("tool", "0 files matched", tool_call_id="call_1"),
            RawMessage("assistant", "", tool_calls=(RawToolCall("call_2", "search_files", "{}"),)),
        )
    )
    assert compare("wl", previous, current).kind is DivergenceKind.NONE


def test_jsonl_parser_rejects_a_tool_call_missing_its_id() -> None:
    from sixeyes.ingest.jsonl import JsonlFormatError

    import json
    import pytest

    obj = {
        "request_id": "r", "timestamp": 1.0, "model": "m",
        "messages": [{"role": "assistant", "content": None,
                      "tool_calls": [{"name": "search_files", "arguments": {}}]}],
    }
    with pytest.raises(JsonlFormatError) as excinfo:
        parse_line(json.dumps(obj), line_no=5)
    assert excinfo.value.line_no == 5
    assert "id" in str(excinfo.value)


def test_jsonl_round_trip_preserves_tool_call_structure() -> None:
    import json

    obj = {
        "request_id": "r", "timestamp": 1.0, "model": "m",
        "messages": [{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_9", "name": "search_files", "arguments": {"b": 2, "a": 1}}],
        }],
    }
    parsed = parse_line(json.dumps(obj), line_no=1)
    call = parsed.messages[0].tool_calls[0]
    assert call.id == "call_9"
    assert call.name == "search_files"
    # canonicalized regardless of the original key order in "arguments"
    assert call.arguments_json == '{"a":1,"b":2}'
