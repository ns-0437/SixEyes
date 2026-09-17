"""Offline integration test: the REAL AgentFuse adapter (`guarded_tool_loop`) driven by
SixEyes's own fake client -- proving compatibility with actual adapter output, not just
with a reimplementation of its shape.

`tests/test_agentfuse_bridge.py` exercises `pilots.agentfuse.bridge.run_scripted_loop`,
SixEyes's own reimplementation of `guarded_tool_loop`'s message-building logic. An
independent review correctly pointed out that this establishes synthetic pipeline
behaviour, not compatibility with the real adapter -- reading source and copying its shape
is not the same claim as running the actual function. This file closes that gap.

Requires a local AgentFuse checkout: set `AGENTFUSE_PATH` to the checkout's root (the
directory containing the `agentfuse` package) to run these tests; they are skipped
otherwise, with a clear reason. This is deliberately a configurable, opt-in checkout, not
a hardcoded developer-specific path and not a production dependency of SixEyes -- the
import happens only inside this test file, gated on the environment variable. No network
call and no real API key are used anywhere in this file; `ScriptedClient` never leaves
memory.
"""

from __future__ import annotations

import os
import sys

import pytest

_AGENTFUSE_PATH = os.environ.get("AGENTFUSE_PATH")
if _AGENTFUSE_PATH and _AGENTFUSE_PATH not in sys.path:
    sys.path.insert(0, _AGENTFUSE_PATH)

try:
    from agentfuse.adapters.openai_sdk import guarded_tool_loop  # type: ignore[import-not-found]
    from agentfuse.events import EventType  # type: ignore[import-not-found]
    from agentfuse.monitor import Directive, DirectiveKind  # type: ignore[import-not-found]

    _AGENTFUSE_AVAILABLE = True
except ImportError:
    _AGENTFUSE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _AGENTFUSE_AVAILABLE,
    reason=(
        "Real AgentFuse adapter not importable. Set AGENTFUSE_PATH to a local agentfuse "
        "checkout's root (the directory containing the 'agentfuse' package) to run this "
        "offline integration test."
    ),
)

from pilots.agentfuse.bridge import AgentFuseShapeError, convert_captured_calls
from pilots.agentfuse.fake_client import (
    FakeChatCompletion,
    FakeChoice,
    FakeFunction,
    FakeMessage,
    FakeToolCall,
    FakeUsage,
    ScriptedClient,
)

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


def _router(name: str, args: dict) -> str:
    return "0 files matched"


def test_real_adapter_without_tools_converts_cleanly() -> None:
    """The simplest real path: no tools, so guarded_tool_loop never adds `tool_choice`
    (openai_sdk.py only sets it `if tools:`) -- nothing for the bridge to reject. Confirms
    the real adapter's plain text-turn shape converts successfully end to end."""
    client = ScriptedClient([_text_response("all done")])
    result = guarded_tool_loop(
        client, model="gpt-4o", system_prompt="You are a helpful agent.",
        user_input="say hi", tools=[], tool_router=_router, max_turns=3,
    )
    assert result["status"] == "complete"
    assert len(client.captured) == 1

    trace = convert_captured_calls(client.captured, workload_id="wl_real_adapter")
    assert len(trace.requests) == 1
    request = trace.requests[0]
    assert request.system == "You are a helpful agent."
    assert request.messages[0].content == "say hi"


def test_real_adapter_with_tools_makes_a_real_tool_call_and_is_explicitly_rejected() -> None:
    """With tools present, the real adapter always sends `tool_choice`
    (openai_sdk.py:68) -- a field RawRequest has no slot for and that this bridge
    therefore cannot silently ignore. The correct behaviour is an explicit, clear
    rejection, not a silent drop or a corrupted conversion -- proven here against
    genuine kwargs the real function actually produced, not a copy of its shape."""
    client = ScriptedClient(
        [_tool_call_response("call_1", "search_files", '{"pattern":"*.conn"}'), _text_response("found nothing")]
    )
    result = guarded_tool_loop(
        client, model="gpt-4o", system_prompt="sys", user_input="find it",
        tools=TOOLS, tool_router=_router, max_turns=3,
    )
    assert result["status"] == "complete"
    assert len(client.captured) == 2
    # confirms the premise: the real adapter really did send tool_choice
    assert client.captured[0].extra_kwargs.get("tool_choice") == "auto"
    # and the real second captured call really does carry the tool-call turn, with the
    # argument string preserved exactly as the fake model "emitted" it
    assert client.captured[1].messages[-2]["tool_calls"][0]["function"]["arguments"] == '{"pattern":"*.conn"}'

    with pytest.raises(AgentFuseShapeError, match="tool_choice"):
        convert_captured_calls(client.captured, workload_id="wl_real_adapter")


def test_real_adapters_rerun_restart_actually_truncates_the_captured_history() -> None:
    """Forces the real `_apply_directive` 'rerun' branch (the adapter's default
    intervention) via a minimal fake monitor -- exercising the actual restart/retry
    mechanics AgentFuse ships, not an assumption about how they work. The call captured
    immediately after the restart must show messages truncated back to
    [system, user, steer], proving the real adapter's documented discard-the-failing-
    turns behaviour, independent of anything this bridge does with it afterward."""

    class _RestartOnceMonitor:
        """`monitor` is duck-typed by guarded_tool_loop (Optional[CircuitBreakerMonitor],
        never isinstance-checked) -- this stands in for it, returning exactly one
        INJECT/rerun directive right after the first tool result, then CONTINUE for
        everything else, so the restart path fires deterministically instead of depending
        on real detector threshold tuning."""

        def __init__(self) -> None:
            self._restarted = False

        def observe(self, event: object) -> Directive:
            if not self._restarted and getattr(event, "type", None) is EventType.TOOL_RESULT:
                self._restarted = True
                return Directive(kind=DirectiveKind.INJECT, steering_text="stop repeating yourself")
            return Directive(kind=DirectiveKind.CONTINUE)

        def finish(self, status: str = "complete") -> dict:
            return {"status": status}

    client = ScriptedClient(
        [_tool_call_response("call_1", "search_files", "{}"), _text_response("clean run after restart")]
    )
    result = guarded_tool_loop(
        client, model="gpt-4o", system_prompt="sys", user_input="find it",
        tools=TOOLS, tool_router=_router, max_turns=5, monitor=_RestartOnceMonitor(),  # type: ignore[arg-type]
    )
    assert result["status"] == "complete"

    restarted_call = client.captured[-1]
    assert [m["role"] for m in restarted_call.messages] == ["system", "user", "user"]
    assert restarted_call.messages[-1]["content"] == "[CIRCUIT BREAKER STEERING] stop repeating yourself"
