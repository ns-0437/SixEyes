"""Request controls must survive ingestion and stay outside prompt-prefix estimates."""

import json
import pickle
from dataclasses import replace

import pytest

from pilots.agentfuse.bridge import AgentFuseShapeError, InMemoryTraceSource, convert_captured_calls
from pilots.agentfuse.fake_client import FakeChatCompletion, FakeChoice, FakeMessage, ScriptedClient
from sixeyes.fingerprint.divergence import compare
from sixeyes.fingerprint.fingerprint import Fingerprint, fingerprint_request
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.graph import DiskCache, Executor, Graph
from sixeyes.ingest.jsonl import JsonlFormatError, parse_line
from sixeyes.ingest.types import RawRequest, RawToolChoice, RawTrace

KEY = b"\x05" * 32
SECRET = "SYNTHETIC-PRIVATE-NAME"


def capture(extra=None, messages=None, tools=None):
    client = ScriptedClient([FakeChatCompletion((FakeChoice(FakeMessage(content="done")),))])
    client.chat.completions.create(
        model="synthetic", messages=messages if messages is not None else [
            {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
        ], tools=tools if tools is not None else [], **(extra or {}),
    )
    return client.captured


@pytest.mark.parametrize("wire,expected", [
    ("auto", RawToolChoice("auto")), ("none", RawToolChoice("none")),
    ("required", RawToolChoice("required")),
    ({"type": "function", "function": {"name": SECRET}}, RawToolChoice("function", SECRET)),
])
def test_both_ingest_paths_preserve_supported_choices(wire, expected):
    bridged = convert_captured_calls(capture({"tool_choice": wire}), "w").requests[0]
    parsed = parse_line(json.dumps({
        "request_id": "r", "timestamp": 1, "model": "m", "messages": [], "tool_choice": wire,
    }), 1)
    assert bridged.tool_choice == parsed.tool_choice == expected


@pytest.mark.parametrize("choice", [RawToolChoice("auto"), RawToolChoice("none"),
                                    RawToolChoice("required"), RawToolChoice("function", SECRET)])
def test_choice_changes_are_keyed_and_not_misreported_as_prefix_positions(choice):
    base = RawRequest("r", 0.0, "m", "sys", (), ())
    previous = fingerprint_request(base, KEY)
    current = fingerprint_request(replace(base, tool_choice=choice), KEY)
    assert current.segments == previous.segments
    report = compare("w", previous, current)
    assert report.kind is DivergenceKind.TOOL_CHOICE_CHANGED
    assert report.cache_missed_units == 0
    assert SECRET.encode() not in pickle.dumps(current)
    assert current.tool_choice_digest != fingerprint_request(replace(base, tool_choice=choice), b"\x06" * 32).tool_choice_digest
    assert compare("w", current, fingerprint_request(replace(base, tool_choice=choice), KEY)).kind is DivergenceKind.NONE
    assert compare("w", current, previous).kind is DivergenceKind.TOOL_CHOICE_CHANGED


@pytest.mark.parametrize("wire", [None, 1, [], {}, SECRET, {SECRET: "auto"},
    {"type": "function", "function": {"name": "x", SECRET: "extra"}},
    {"type": "allowed_tools", "allowed_tools": []}])
def test_bad_choice_shapes_are_rejected_without_echoing_input(wire):
    with pytest.raises(AgentFuseShapeError) as error:
        convert_captured_calls(capture({"tool_choice": wire}), "w")
    assert SECRET not in str(error.value)
    with pytest.raises(JsonlFormatError) as error:
        parse_line(json.dumps({"request_id": "r", "timestamp": 1, "model": "m",
                               "messages": [], "tool_choice": wire}), 1)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("location", ["top", "message", "system", "tool", "function"])
def test_unknown_key_names_are_also_sensitive(location):
    calls = capture()
    call = calls[0]
    if location == "top":
        call.extra_kwargs[SECRET] = 1
    elif location in ("message", "system"):
        call.messages[location == "message"][SECRET] = 1
    else:
        tool = {"type": "function", "function": {"name": "search"}}
        (tool if location == "tool" else tool["function"])[SECRET] = 1
        call.tools.append(tool)
    with pytest.raises(AgentFuseShapeError) as error:
        convert_captured_calls(calls, "w")
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("field,value", [("messages", "not-an-array"), ("tools", {}), ("model", 123)])
def test_capture_does_not_coerce_invalid_shapes(field, value):
    client = ScriptedClient([FakeChatCompletion((FakeChoice(FakeMessage(content="done")),))])
    kwargs = {"model": "m", "messages": [{"role": "system", "content": "s"}], "tools": []}
    kwargs[field] = value
    client.chat.completions.create(**kwargs)
    with pytest.raises(AgentFuseShapeError):
        convert_captured_calls(client.captured, "w")


async def test_source_trace_binding_cannot_change_through_public_config(tmp_path):
    original = RawTrace("w", (RawRequest("r", 0.0, "m", "first", (), ()),))
    source = InMemoryTraceSource("source", trace=original)
    graph = Graph("pilot")
    graph.add(source)
    graph.add(Fingerprint("fp", key=KEY), trace="source")
    executor = Executor(cache=DiskCache(tmp_path))
    first = await executor.run(graph)
    source.config["trace"] = RawTrace("w", (replace(original.requests[0], system="second"),))
    second = await executor.run(graph)
    assert second["source"] == original
    assert second["fp"] == first["fp"]
