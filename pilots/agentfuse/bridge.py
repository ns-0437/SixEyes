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
this reimplementation can go stale -- `tests/test_agentfuse_real_adapter.py` runs the
actual `guarded_tool_loop` against this same fake client to catch that drift, but only
when `AGENTFUSE_PATH` is set to a local checkout; it is opt-in, not part of the default
suite or CI, for the same reason this module doesn't import the package directly.

Every rejection below is explicit (`AgentFuseShapeError`), never a silent drop or a
best-effort coercion: a shape this bridge doesn't understand is a bug to fix or a real
gap to report, not something to guess past. Error messages describe *field paths and
expected shapes only* -- position, key name, expected type or allowed-value set -- and
never the rejected value itself. An earlier version of this module interpolated the
actual rejected value (a role string, a `type` field) into these messages with `!r`; an
independent review demonstrated a planted sensitive marker surviving verbatim into the
raised exception. A field that *looks* like a small fixed enum (role, type) is still
untrusted input until validated, and the error path runs before any redaction the
executor might otherwise apply -- so this module must not rely on that later layer.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, ClassVar

from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.jsonl import canonical_json
from sixeyes.ingest.tool_choice import parse_tool_choice
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolCall, RawToolDef, RawTrace
from pilots.agentfuse.fake_client import CapturedCall, FakeChatCompletion, ScriptedClient

# Keys this bridge has an explicit, tested mapping for. Anything else present on a
# captured object is rejected rather than silently ignored -- see the module docstring on
# `extra_kwargs` and the "unsupported fields disappear" fix this set exists to close.
_SUPPORTED_MESSAGE_KEYS = frozenset({"role", "content", "tool_call_id", "tool_calls"})
_SUPPORTED_TOOL_KEYS = frozenset({"type", "function"})
_SUPPORTED_TOOL_FUNCTION_KEYS = frozenset({"name", "description", "parameters"})
_SUPPORTED_TOOL_CALL_KEYS = frozenset({"id", "type", "function"})
_SUPPORTED_TOOL_CALL_FUNCTION_KEYS = frozenset({"name", "arguments"})


class AgentFuseShapeError(ValueError):
    """A captured call did not match a shape this bridge explicitly supports. Raised
    instead of silently dropping the unsupported part or guessing a mapping for it. Never
    constructed with the rejected value itself in its message -- only field paths, key
    names, and expected shapes, all of which come from this module's own fixed schema
    knowledge, not from customer-controlled data."""


def _reject_unexpected_keys(obj: dict[str, Any], supported: frozenset[str], path: str) -> None:
    """Reject unknown keys without echoing them: key names are also arbitrary input."""
    if not isinstance(obj, dict):
        raise AgentFuseShapeError(f"{path}: expected an object")
    extra = set(obj.keys()) - supported
    if extra:
        raise AgentFuseShapeError(
            f"{path}: unsupported field(s) -- supported fields are "
            f"{sorted(supported)!r}"
        )


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
    path = f"call {call_index} tool {tool_index}"
    _reject_unexpected_keys(tool, _SUPPORTED_TOOL_KEYS, path)
    if tool.get("type") != "function":
        raise AgentFuseShapeError(f"{path}.type: unsupported value -- only 'function' is supported")
    fn = tool.get("function")
    if not isinstance(fn, dict):
        raise AgentFuseShapeError(f"{path}.function: must be an object")
    _reject_unexpected_keys(fn, _SUPPORTED_TOOL_FUNCTION_KEYS, f"{path}.function")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise AgentFuseShapeError(f"{path}.function.name: must be a non-empty string")
    description = fn.get("description", "")
    if not isinstance(description, str):
        raise AgentFuseShapeError(f"{path}.function.description: must be a string")
    parameters = fn.get("parameters", {})
    if not isinstance(parameters, dict):
        raise AgentFuseShapeError(f"{path}.function.parameters: expected an object")
    try:
        schema = canonical_json(parameters)
    except (TypeError, ValueError, RecursionError):
        raise AgentFuseShapeError(f"{path}.function.parameters: expected JSON data") from None
    return RawToolDef(name=name, description=description, schema_json=schema)


def _convert_tool_call(tc: dict[str, Any], call_index: int, msg_index: int, tc_index: int) -> RawToolCall:
    path = f"call {call_index} message {msg_index} tool_call {tc_index}"
    _reject_unexpected_keys(tc, _SUPPORTED_TOOL_CALL_KEYS, path)
    if tc.get("type") != "function":
        raise AgentFuseShapeError(f"{path}.type: unsupported value -- only 'function' is supported")
    tc_id = tc.get("id")
    if not isinstance(tc_id, str) or not tc_id:
        raise AgentFuseShapeError(f"{path}.id: must be a non-empty string")
    fn = tc.get("function")
    if not isinstance(fn, dict):
        raise AgentFuseShapeError(f"{path}.function: must be an object")
    _reject_unexpected_keys(fn, _SUPPORTED_TOOL_CALL_FUNCTION_KEYS, f"{path}.function")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise AgentFuseShapeError(f"{path}.function.name: must be a non-empty string")
    arguments = fn.get("arguments")
    # Must be a string, never parsed -- see RawToolCall.arguments_raw's docstring. A
    # nested object here would mean this bridge (or its fixture) built the wrong shape;
    # reject rather than silently str()-coerce it into something that looks like JSON but
    # isn't what the model actually produced.
    if not isinstance(arguments, str):
        raise AgentFuseShapeError(
            f"{path}.function.arguments: must be a string (got type {type(arguments).__name__})"
        )
    return RawToolCall(id=tc_id, name=name, arguments_raw=arguments)


def _convert_message(message: dict[str, Any], call_index: int, msg_index: int) -> RawMessage:
    path = f"call {call_index} message {msg_index}"
    _reject_unexpected_keys(message, _SUPPORTED_MESSAGE_KEYS, path)
    role = message.get("role")
    if role not in ("user", "assistant", "tool"):
        raise AgentFuseShapeError(
            f"{path}.role: unsupported value -- expected one of 'user', 'assistant', 'tool'"
        )
    content = message.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        # A real OpenAI-style multimodal message (a list of content blocks) would land
        # here -- AgentFuse's own adapter never produces one, but this bridge must not
        # silently str() it into something that looks like text and isn't.
        raise AgentFuseShapeError(f"{path}.content: must be a string or null (got type {type(content).__name__})")
    raw_tool_calls = message.get("tool_calls", [])
    if not isinstance(raw_tool_calls, list):
        raise AgentFuseShapeError(f"{path}.tool_calls: expected an array")
    if raw_tool_calls and role != "assistant":
        raise AgentFuseShapeError(f"{path}.tool_calls: only supported on assistant messages")
    tool_call_id = message.get("tool_call_id")
    if tool_call_id is not None and (not isinstance(tool_call_id, str) or not tool_call_id):
        raise AgentFuseShapeError(f"{path}.tool_call_id: expected a non-empty string")
    if role == "tool" and tool_call_id is None:
        raise AgentFuseShapeError(f"{path}.tool_call_id: required on tool results")
    if role != "tool" and tool_call_id is not None:
        raise AgentFuseShapeError(f"{path}.tool_call_id: only supported on tool results")
    tool_calls = tuple(
        _convert_tool_call(tc, call_index, msg_index, i) for i, tc in enumerate(raw_tool_calls)
    )
    return RawMessage(role=role, content=content, tool_call_id=tool_call_id, tool_calls=tool_calls)


def _convert_call(call: CapturedCall, workload_id: str, index: int) -> RawRequest:
    path = f"call {index}"
    _reject_unexpected_keys(call.extra_kwargs, frozenset({"tool_choice"}), path)
    try:
        tool_choice = (parse_tool_choice(call.extra_kwargs["tool_choice"])
                       if "tool_choice" in call.extra_kwargs else None)
    except ValueError:
        raise AgentFuseShapeError(f"{path}.tool_choice: unsupported shape") from None
    if not isinstance(call.model, str) or not call.model:
        raise AgentFuseShapeError(f"{path}.model: expected a non-empty string")
    if not isinstance(call.messages, list):
        raise AgentFuseShapeError(f"{path}.messages: expected an array")
    if not isinstance(call.tools, list):
        raise AgentFuseShapeError(f"{path}.tools: expected an array")
    if not call.messages:
        raise AgentFuseShapeError(f"{path}.messages[0]: expected a system message, found no messages")
    _reject_unexpected_keys(call.messages[0], frozenset({"role", "content"}), f"{path}.messages[0]")
    if call.messages[0].get("role") != "system":
        raise AgentFuseShapeError(f"{path}.messages[0].role: expected 'system'")
    for later_index, message in enumerate(call.messages[1:], start=1):
        _reject_unexpected_keys(message, _SUPPORTED_MESSAGE_KEYS, f"{path}.messages[{later_index}]")
        if message.get("role") == "system":
            raise AgentFuseShapeError(
                f"{path}.messages[{later_index}]: a second system message is not supported "
                f"(system must be exactly messages[0])"
            )
    system_content = call.messages[0].get("content")
    if not isinstance(system_content, str):
        raise AgentFuseShapeError(f"{path}.messages[0].content: must be a string")

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
        tool_choice=tool_choice,
    )


def convert_captured_calls(captured: list[CapturedCall], workload_id: str) -> RawTrace:
    """The actual shim: a sequence of captured, in-memory request snapshots -> RawTrace.
    Never touches disk. Raises `AgentFuseShapeError` on the first call it cannot map
    faithfully, rather than skipping it or guessing."""
    requests = tuple(_convert_call(call, workload_id, i) for i, call in enumerate(captured))
    return RawTrace(workload_id=workload_id, requests=requests)


class InMemoryTraceSource(Node):
    """A SOURCE node wrapping a RawTrace already built in memory -- no file, no network.
    Construction argument: `trace` (RawTrace), held outside mutable node config.
    Content-bearing, same as JsonlSource. Create a new source for a different trace.

    `config_key()` is a random UUID generated once at construction, not `id(self)`. An
    earlier version used `id(self)` as cache identity; an independent review demonstrated
    concretely why that's wrong: CPython reuses an object's memory address after it is
    garbage collected, so a *freshly constructed* source for a brand-new trace can end up
    with the same `id()` a previous, now-dead source had -- and downstream nodes keyed off
    that identity (Fingerprint) then serve the previous trace's cached fingerprints for
    genuinely different content. A `uuid.uuid4()` nonce has no relationship to memory
    layout and is generated fresh per instance, so two sources are never confused even
    under address reuse. It is still not a content hash (hashing raw customer content into
    a cache-key string would itself be a persistence-boundary concern, CLAUDE.md rule 3) --
    it is only guaranteed unique per *instance*, which is what this pilot-only node needs:
    it is always constructed fresh with exactly one trace, never rebound to a different one
    after construction.
    """

    kind = NodeKind.SOURCE
    version = "3"  # nonce bound to construction-time trace outside mutable config
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    def __init__(self, node_id: str, *, trace: RawTrace) -> None:
        super().__init__(node_id)
        self._trace = trace
        self._nonce = uuid.uuid4().hex

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        trace = self._trace
        ctx.log("in-memory trace with %d requests, no file or network involved", len(trace.requests))
        return trace

    def config_key(self) -> Any:
        return {"kind": "in_memory_trace_source", "nonce": self._nonce}
