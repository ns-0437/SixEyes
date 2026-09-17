"""Offline acceptance tests for the AgentFuse pilot's memory-only capture shim.

Entirely synthetic: `ScriptedClient` makes no network call, and no JSONL file is ever
written -- `run_scripted_loop` drives a fake client directly, `convert_captured_calls`
builds `RawRequest` objects straight into memory, and `InMemoryTraceSource` feeds them into
the real Fingerprint/Divergence pipeline through the real Graph/Executor. This is what
"actual AgentFuse request shapes -> SixEyes -> divergence report" means without touching
any real API, model, or customer data -- per the explicit instruction that live execution
stays disabled until task, model, call-limit, and spending controls are approved separately.
"""

from __future__ import annotations

import gc
import importlib
import weakref
from pathlib import Path
from typing import Any

import pytest

from pilots.agentfuse.bridge import (
    AgentFuseShapeError,
    InMemoryTraceSource,
    convert_captured_calls,
    run_scripted_loop,
)
from pilots.agentfuse.fake_client import (
    FakeChatCompletion,
    FakeChoice,
    FakeFunction,
    FakeMessage,
    FakeToolCall,
    FakeUsage,
    ScriptedClient,
)
from sixeyes.fingerprint.divergence import Divergence, compare
from sixeyes.fingerprint.fingerprint import Fingerprint, fingerprint_request
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.graph import DiskCache, Executor, Graph
from sixeyes.ingest.types import RawRequest, RawTrace

TEST_KEY = b"\x09" * 32
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "search the repo",
            "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
        },
    }
]


def _text_response(text: str) -> FakeChatCompletion:
    return FakeChatCompletion(
        choices=(FakeChoice(message=FakeMessage(content=text)),),
        usage=FakeUsage(prompt_tokens=10, completion_tokens=5),
    )


def _tool_call_response(call_id: str, name: str, arguments: str) -> FakeChatCompletion:
    return FakeChatCompletion(
        choices=(
            FakeChoice(
                message=FakeMessage(
                    content="", tool_calls=(FakeToolCall(id=call_id, function=FakeFunction(name, arguments)),)
                )
            ),
        ),
        usage=FakeUsage(prompt_tokens=20, completion_tokens=8),
    )


def _pipeline(workload_id: str, trace: Any) -> Graph:
    graph = Graph("agentfuse_pilot")
    graph.add(InMemoryTraceSource("source", trace=trace))
    graph.add(Fingerprint("fingerprint", key=TEST_KEY), trace="source")
    graph.add(Divergence("divergence", workload_id=workload_id), fingerprints="fingerprint")
    return graph


def _noop_router(name: str, args: dict[str, Any]) -> str:
    return "0 files matched"


async def test_full_pipeline_reports_none_for_an_unchanged_prefix_then_appended_growth(
    tmp_path: Path,
) -> None:
    """One scripted loop: a tool-call turn followed by a final text turn. The second
    captured call's messages are the first call's messages plus new turns appended --
    exactly the append-only growth case -- so comparing them must report NONE, run through
    the real Executor and Graph, not just fingerprint_request() directly."""
    client = ScriptedClient(
        [
            _tool_call_response("call_1", "search_files", '{"pattern":"*.conn"}'),
            _text_response("Found nothing."),
        ]
    )
    run_scripted_loop(
        client,
        _noop_router,
        model="gpt-4o",
        system_prompt="You are a helpful agent.",
        user_input="find the connection string",
        tools=TOOLS,
    )
    trace = convert_captured_calls(client.captured, workload_id="wl_agentfuse")
    assert len(trace.requests) == 2

    graph = _pipeline("wl_agentfuse", trace)
    result = await Executor(cache=DiskCache(root=tmp_path / "cache")).run(graph, targets=["divergence"])
    reports = result["divergence"]
    assert len(reports) == 1
    assert reports[0].kind is DivergenceKind.NONE


async def test_tool_call_id_name_and_argument_text_are_preserved_through_the_shim() -> None:
    """The exact original argument string -- not a canonicalized re-serialization -- must
    survive capture -> conversion -> fingerprinting, matching the real wire shape."""
    client = ScriptedClient(
        [_tool_call_response("call_7", "search_files", '{"pattern":  "*.env"}'), _text_response("done")]
    )
    run_scripted_loop(client, _noop_router, model="gpt-4o", system_prompt="sys", user_input="go", tools=TOOLS)
    trace = convert_captured_calls(client.captured, workload_id="wl")
    # the SECOND captured call is the one sent after the tool-call turn was appended --
    # that's where the tool call shows up in the message history.
    call = trace.requests[1].messages[-2].tool_calls[0]
    assert call.id == "call_7"
    assert call.name == "search_files"
    assert call.arguments_raw == '{"pattern":  "*.env"}'


async def test_two_tool_calls_in_one_turn_keep_distinct_ordering_and_result_binding() -> None:
    """A real assistant turn can request multiple tools at once, each answered by its own
    later tool-role message carrying a matching tool_call_id. Swapping which result answers
    which call must be a detectable structural change -- proving the binding isn't just
    positional."""
    assistant_turn = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "search_files", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ],
    }
    base_messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        assistant_turn,
    ]

    client_normal = ScriptedClient([_text_response("done")])
    client_normal.chat.completions.create(
        model="m",
        messages=base_messages
        + [
            {"role": "tool", "tool_call_id": "call_a", "content": "result for search_files"},
            {"role": "tool", "tool_call_id": "call_b", "content": "result for read_file"},
        ],
        tools=TOOLS,
    )
    baseline_trace = convert_captured_calls(client_normal.captured, workload_id="wl")

    # Same assistant turn, same two results, but the tool_call_id bindings SWAPPED --
    # exactly the kind of bug a customer's own retry/ordering logic could introduce.
    client_swapped = ScriptedClient([_text_response("done")])
    client_swapped.chat.completions.create(
        model="m",
        messages=base_messages
        + [
            {"role": "tool", "tool_call_id": "call_b", "content": "result for search_files"},
            {"role": "tool", "tool_call_id": "call_a", "content": "result for read_file"},
        ],
        tools=TOOLS,
    )
    swapped_trace = convert_captured_calls(client_swapped.captured, workload_id="wl")

    from sixeyes.fingerprint.fingerprint import fingerprint_request

    fp_before = fingerprint_request(baseline_trace.requests[-1], TEST_KEY)
    fp_swapped = fingerprint_request(swapped_trace.requests[-1], TEST_KEY)
    assert fp_before.segment("messages").chain != fp_swapped.segment("messages").chain


async def test_a_restart_that_truncates_history_is_reported_not_silently_allowed() -> None:
    """AgentFuse's own adapter can abandon a turn and rebuild `messages` back down to
    [system, user, ...] -- a real, intentional discontinuity, not an edit within one
    growing history. SixEyes has no special-case for this: it must show up as a genuine
    structural change (messages_changed), the same way any other truncation does, because
    this analyzer cannot know whether the customer's own cache breakpoint survives a
    restart."""
    client = ScriptedClient(
        [
            _tool_call_response("call_1", "search_files", "{}"),
            _text_response("first attempt done"),
        ]
    )
    run_scripted_loop(
        client, _noop_router, model="gpt-4o", system_prompt="sys", user_input="task one", tools=TOOLS
    )
    before_restart = convert_captured_calls(client.captured, workload_id="wl")

    # A restart: a fresh loop invocation, same client, but starting over from
    # [system, user] with new content -- exactly AgentFuse's own rerun behaviour.
    client.captured.clear()
    client.load_script([_text_response("second attempt, clean slate")])
    run_scripted_loop(
        client, _noop_router, model="gpt-4o", system_prompt="sys", user_input="task two, restarted", tools=TOOLS
    )
    after_restart = convert_captured_calls(client.captured, workload_id="wl")

    from sixeyes.fingerprint.fingerprint import fingerprint_request

    fp_before = fingerprint_request(before_restart.requests[-1], TEST_KEY)
    fp_after = fingerprint_request(after_restart.requests[-1], TEST_KEY)
    report = compare("wl", fp_before, fp_after)
    assert report.kind is not DivergenceKind.NONE


async def test_missing_system_message_is_rejected_not_silently_skipped() -> None:
    client = ScriptedClient([_text_response("hi")])
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hello"}], tools=[])
    with pytest.raises(AgentFuseShapeError, match="system"):
        convert_captured_calls(client.captured, workload_id="wl")


async def test_tool_with_unsupported_type_is_rejected_not_silently_dropped() -> None:
    client = ScriptedClient([_text_response("hi")])
    client.chat.completions.create(
        model="m",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=[{"type": "code_interpreter", "function": {"name": "x"}}],
    )
    with pytest.raises(AgentFuseShapeError, match=r"tool 0\.type"):
        convert_captured_calls(client.captured, workload_id="wl")


async def test_non_string_message_content_is_rejected_not_silently_coerced() -> None:
    """A real OpenAI-style multimodal message (content: list of blocks) must not be
    silently str()-coerced into something that looks like text but corrupts the
    fingerprint."""
    client = ScriptedClient([_text_response("hi")])
    client.chat.completions.create(
        model="m",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ],
        tools=[],
    )
    with pytest.raises(AgentFuseShapeError, match="content"):
        convert_captured_calls(client.captured, workload_id="wl")


async def test_non_string_tool_call_arguments_are_rejected_not_canonicalized() -> None:
    """If a captured call somehow carries arguments as a parsed object rather than the
    real string form, this must fail loudly -- silently canonicalizing it is exactly the
    erasure the arguments_raw fix exists to prevent."""
    client = ScriptedClient([_text_response("hi")])
    client.chat.completions.create(
        model="m",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": {"a": 1}}}
                ],
            },
        ],
        tools=[],
    )
    with pytest.raises(AgentFuseShapeError, match="arguments"):
        convert_captured_calls(client.captured, workload_id="wl")


async def test_no_raw_content_leaks_into_a_rejection_error_message() -> None:
    """A rejection error must describe shape (role, type, position), never echo the actual
    customer content that triggered it -- the same discipline ingest.jsonl already follows
    for its own errors."""
    secret = "sk-super-secret-do-not-leak-this-token"
    client = ScriptedClient([_text_response("hi")])
    client.chat.completions.create(
        model="m",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": [secret]}],
        tools=[],
    )
    with pytest.raises(AgentFuseShapeError) as excinfo:
        convert_captured_calls(client.captured, workload_id="wl")
    assert secret not in str(excinfo.value)


async def test_no_jsonl_file_is_ever_written(tmp_path: Path, monkeypatch: Any) -> None:
    """Confirms the memory-only claim directly: run a full scripted loop and pipeline, then
    assert nothing new appeared on disk outside the executor's own cache directory."""
    before = set(tmp_path.rglob("*"))
    client = ScriptedClient([_text_response("hi")])
    run_scripted_loop(client, _noop_router, model="m", system_prompt="s", user_input="u", tools=[], max_turns=1)
    trace = convert_captured_calls(client.captured, workload_id="wl")
    graph = _pipeline("wl", trace)
    cache_dir = tmp_path / "cache"
    await Executor(cache=DiskCache(root=cache_dir)).run(graph, targets=["divergence"])

    after_non_cache = {p for p in tmp_path.rglob("*") if cache_dir not in p.parents and p != cache_dir}
    assert after_non_cache == before


# --- Regressions from an independent review of 7bfdf9d, 2026-09-17 -------------------
#
# Three real defects, each reproduced against the live repository before being fixed:
# InMemoryTraceSource's cache identity could collide after CPython reuses a garbage-
# collected object's id(); rejection errors echoed the actual rejected value instead of
# just describing the field and expected shape; and the fake client silently dropped any
# kwarg outside model/messages/tools (tool_choice), while the converter silently ignored
# unrecognized nested keys (a tool's "strict", a message's "name") instead of rejecting
# them, contradicting the bridge's own explicit-rejection contract.


async def test_fresh_sources_do_not_collide_after_object_id_reuse(tmp_path: Path, monkeypatch: Any) -> None:
    """Deterministically model CPython reusing a dead object's address for a brand-new
    one -- this does not assign equal ids to two simultaneously live sources, only to a
    dead one and a later one, which is exactly what happens in a long-running pilot
    process across many traces. Before the fix (id(self) as cache identity), the second,
    unrelated trace's Fingerprint output came back as the first trace's cached segments."""
    bridge = importlib.import_module("pilots.agentfuse.bridge")
    monkeypatch.setattr(bridge, "id", lambda obj: 123456, raising=False)

    def trace(system: str) -> RawTrace:
        return RawTrace("workload", (RawRequest("r", 1.0, "m", system, (), ()),))

    def graph_for(source: InMemoryTraceSource) -> Graph:
        graph = Graph("pilot")
        graph.add(source)
        graph.add(Fingerprint("fp", key=TEST_KEY), trace="source")
        return graph

    executor = Executor(cache=DiskCache(tmp_path / "cache"))
    first_source = InMemoryTraceSource("source", trace=trace("alpha"))
    first_ref = weakref.ref(first_source)
    await executor.run(graph_for(first_source))
    del first_source
    gc.collect()
    assert first_ref() is None  # confirms the old source is actually dead, not just unused

    second_trace = trace("bravo")
    second_source = InMemoryTraceSource("source", trace=second_trace)
    second = await executor.run(graph_for(second_source))
    assert second["source"].requests[0].system == "bravo"
    expected = fingerprint_request(second_trace.requests[0], TEST_KEY)
    assert second["fp"][0].segments == expected.segments


@pytest.mark.parametrize("field", ["role", "tool_type", "tool_call_type"])
def test_rejected_enum_values_are_never_echoed_in_the_error(field: str) -> None:
    """A field that looks like a small fixed enum (role, tool type) is still untrusted
    input until validated -- a planted sensitive marker used as the value must never
    appear in the resulting AgentFuseShapeError, only a description of what was expected."""
    secret = "SYNTHETIC-SENSITIVE-VALUE"
    client = ScriptedClient([_text_response("done")])
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    tools: list[dict[str, Any]] = []
    if field == "role":
        messages.append({"role": secret, "content": "x"})
    elif field == "tool_type":
        tools = [{"type": secret}]
    else:
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": secret}]})
    client.chat.completions.create(model="m", messages=messages, tools=tools)
    with pytest.raises(AgentFuseShapeError) as excinfo:
        convert_captured_calls(client.captured, workload_id="wl")
    assert secret not in str(excinfo.value)
