"""AgentFuse pilot: memory-only capture shim.

Converts a scripted, in-memory tool-use conversation (via `fake_client.ScriptedClient`,
no network) into a `sixeyes.ingest.types.RawTrace` -- entirely in memory, no JSONL file
ever written, no real API call.

Why this doesn't import the `agentfuse` package: `agentfuse` lives in a separate sibling
repository at a machine-specific path, and SixEyes is meant to stay provider- and
framework-agnostic (CLAUDE.md rule 9 -- dependencies are a liability). Adding a filesystem
path import here would couple two independent projects' lifecycles and break for anyone
without that exact local layout. Instead, `run_scripted_loop` below is SixEyes's own
reimplementation of the *shape* of `agentfuse.adapters.openai_sdk.guarded_tool_loop`'s
message-building logic -- verified by reading that function's real source on 2026-09-17,
not guessed -- so the fixtures this pilot tests against are faithful to a real integration
target without creating a dependency on it. If AgentFuse's real adapter changes shape,
this reimplementation can go stale; it is not exercised against the real package in CI.

Every rejection below is explicit (`AgentFuseShapeError`), never a silent drop or a
best-effort coercion: a shape this bridge doesn't understand is a bug to fix or a real
gap to report, not something to guess past. Error messages describe *shape* (role names,
types, positions) only -- never the actual content of a message or argument -- matching
the same no-raw-content-in-errors discipline `ingest.jsonl._require_float` already
follows.
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar

from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.jsonl import canonical_json
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolCall, RawToolDef, RawTrace
from pilots.agentfuse.fake_client import CapturedCall, FakeChatCompletion, ScriptedClient


class AgentFuseShapeError(ValueError):
    """A captured call did not match a shape this bridge explicitly supports. Raised
    instead of silently dropping the unsupported part or guessing a mapping for it."""


def run_scripted_loop(
    client: ScriptedClient,
    tool_router: Callable[[str, dict[str, Any]], object],
    *,
    model: str,
    system_prompt: str,
    user_input: str,
    tools: list[dict[str, Any]],
    max_turns: int = 6,
) -> list[dict[str, Any]]:
    """Drive one scripted tool-use conversation, turn by turn, exactly the way
    `guarded_tool_loop` drives a real one: call the client, and if the response carries
    tool_calls, append the clean-reconstructed assistant message plus each tool's result
    message and loop again; otherwise append the final assistant text and stop.

    Returns the final `messages` list (for a test's own inspection); the actual per-turn
    request shapes are what `client.captured` holds afterward, via ScriptedClient.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    for _ in range(max_turns):
        response: FakeChatCompletion = client.chat.completions.create(
            model=model, messages=messages, tools=tools
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls
        if not tool_calls:
            messages.append({"role": "assistant", "content": message.content or ""})
            return messages
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            result = tool_router(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return messages


def _convert_tool(tool: dict[str, Any], call_index: int, tool_index: int) -> RawToolDef:
    if tool.get("type") != "function":
        raise AgentFuseShapeError(
            f"call {call_index} tool {tool_index}: unsupported tool type {tool.get('type')!r}, "
            f"expected 'function'"
        )
    fn = tool.get("function")
    if not isinstance(fn, dict):
        raise AgentFuseShapeError(f"call {call_index} tool {tool_index}: missing 'function' object")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise AgentFuseShapeError(f"call {call_index} tool {tool_index}: 'function.name' must be a non-empty string")
    description = fn.get("description", "")
    if not isinstance(description, str):
        raise AgentFuseShapeError(f"call {call_index} tool {tool_index}: 'function.description' must be a string")
    return RawToolDef(name=name, description=description, schema_json=canonical_json(fn.get("parameters", {})))


def _convert_tool_call(tc: dict[str, Any], call_index: int, msg_index: int, tc_index: int) -> RawToolCall:
    if tc.get("type") != "function":
        raise AgentFuseShapeError(
            f"call {call_index} message {msg_index} tool_call {tc_index}: unsupported type "
            f"{tc.get('type')!r}, expected 'function'"
        )
    tc_id = tc.get("id")
    if not isinstance(tc_id, str) or not tc_id:
        raise AgentFuseShapeError(f"call {call_index} message {msg_index} tool_call {tc_index}: missing 'id'")
    fn = tc.get("function")
    if not isinstance(fn, dict):
        raise AgentFuseShapeError(
            f"call {call_index} message {msg_index} tool_call {tc_index}: missing 'function' object"
        )
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise AgentFuseShapeError(
            f"call {call_index} message {msg_index} tool_call {tc_index}: 'function.name' must be a "
            f"non-empty string"
        )
    arguments = fn.get("arguments")
    # Must be a string, never parsed -- see RawToolCall.arguments_raw's docstring. A
    # nested object here would mean this bridge (or its fixture) built the wrong shape;
    # reject rather than silently str()-coerce it into something that looks like JSON but
    # isn't what the model actually produced.
    if not isinstance(arguments, str):
        raise AgentFuseShapeError(
            f"call {call_index} message {msg_index} tool_call {tc_index}: 'function.arguments' must "
            f"be a string (got type {type(arguments).__name__})"
        )
    return RawToolCall(id=tc_id, name=name, arguments_raw=arguments)


def _convert_message(message: dict[str, Any], call_index: int, msg_index: int) -> RawMessage:
    role = message.get("role")
    if role not in ("user", "assistant", "tool"):
        raise AgentFuseShapeError(f"call {call_index} message {msg_index}: unsupported role {role!r}")
    content = message.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        # A real OpenAI-style multimodal message (a list of content blocks) would land
        # here -- AgentFuse's own adapter never produces one, but this bridge must not
        # silently str() it into something that looks like text and isn't.
        raise AgentFuseShapeError(
            f"call {call_index} message {msg_index}: 'content' must be a string or null "
            f"(got type {type(content).__name__})"
        )
    raw_tool_calls = message.get("tool_calls") or []
    tool_calls = tuple(
        _convert_tool_call(tc, call_index, msg_index, i) for i, tc in enumerate(raw_tool_calls)
    )
    return RawMessage(role=role, content=content, tool_call_id=message.get("tool_call_id"), tool_calls=tool_calls)


def _convert_call(call: CapturedCall, workload_id: str, index: int) -> RawRequest:
    if not call.messages or call.messages[0].get("role") != "system":
        raise AgentFuseShapeError(
            f"call {index}: expected messages[0] to be the system message, found "
            f"{'no messages' if not call.messages else call.messages[0].get('role')!r}"
        )
    for later_index, message in enumerate(call.messages[1:], start=1):
        if message.get("role") == "system":
            raise AgentFuseShapeError(
                f"call {index}: found a second system message at index {later_index}, which "
                f"this bridge does not support (system must be exactly messages[0])"
            )
    system_content = call.messages[0].get("content")
    if not isinstance(system_content, str):
        raise AgentFuseShapeError(f"call {index}: system message content must be a string")

    messages = tuple(
        _convert_message(m, index, i) for i, m in enumerate(call.messages[1:], start=1)
    )
    tools = tuple(_convert_tool(t, index, i) for i, t in enumerate(call.tools))

    usage = call.response.usage
    return RawRequest(
        request_id=f"{workload_id}_{index}",
        timestamp=call.captured_at,
        model=call.model,
        system=system_content,
        tools=tools,
        messages=messages,
        usage_input_tokens=usage.prompt_tokens if usage is not None else None,
        usage_output_tokens=usage.completion_tokens if usage is not None else None,
        usage_cache_read_tokens=None,
    )


def convert_captured_calls(captured: list[CapturedCall], workload_id: str) -> RawTrace:
    """The actual shim: a sequence of captured, in-memory request snapshots -> RawTrace.
    Never touches disk. Raises `AgentFuseShapeError` on the first call it cannot map
    faithfully, rather than skipping it or guessing."""
    requests = tuple(_convert_call(call, workload_id, i) for i, call in enumerate(captured))
    return RawTrace(workload_id=workload_id, requests=requests)


class InMemoryTraceSource(Node):
    """A SOURCE node wrapping a RawTrace already built in memory -- no file, no network.
    Config: `trace` (RawTrace). Content-bearing, same as JsonlSource.

    `config_key()` uses `id(self)` rather than hashing the trace's content: hashing raw
    customer content into a cache-key string would itself be a small persistence-boundary
    violation (CLAUDE.md rule 3), and per-instance identity is sufficient here because this
    node is pilot-only, always constructed fresh with one trace, and never reused across
    distinct traces on one instance -- a deliberate, documented simplification, not a
    general solution for a node meant to be cached across runs.
    """

    kind = NodeKind.SOURCE
    version = "1"
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        trace: RawTrace = self.config["trace"]
        ctx.log("in-memory trace with %d requests, no file or network involved", len(trace.requests))
        return trace

    def config_key(self) -> Any:
        return {"kind": "in_memory_trace_source", "instance": id(self)}
