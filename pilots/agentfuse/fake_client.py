"""A duck-typed stand-in for an OpenAI-style chat client -- enough surface for a
tool-calling loop to run against, never a real network call.

This module does not import the `openai` package or the `agentfuse` package. It exists so
`bridge.run_scripted_loop` can drive a multi-turn tool-use conversation entirely in
memory, scripted from a fixed sequence of responses, and so `bridge.convert_captured_calls`
has real request kwargs (not hand-built RawRequest objects) to convert from -- the whole
point being "actual request shapes -> SixEyes", not shapes guessed to fit the schema.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any


class ScriptExhaustedError(RuntimeError):
    """The scripted loop asked for more responses than the fixture provided -- a fixture
    bug, not something to paper over by looping the last response silently."""


@dataclass(frozen=True, slots=True)
class FakeFunction:
    name: str
    arguments: str
    """Exactly as a real model would emit it: a raw string, not parsed. May be invalid
    JSON -- see RawToolCall.arguments_raw's docstring for why that must be preserved."""


@dataclass(frozen=True, slots=True)
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass(frozen=True, slots=True)
class FakeMessage:
    content: str | None = None
    tool_calls: tuple[FakeToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class FakeChoice:
    message: FakeMessage


@dataclass(frozen=True, slots=True)
class FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class FakeChatCompletion:
    choices: tuple[FakeChoice, ...]
    usage: FakeUsage | None = None


@dataclass(frozen=True, slots=True)
class CapturedCall:
    """One snapshot of the exact kwargs a real `client.chat.completions.create(**kwargs)`
    call would have received, paired with the scripted response returned for it.

    `messages` and `tools` are deep-copied at capture time, before the caller mutates its
    own `messages` list for the next turn -- without that, every captured call would end
    up pointing at the SAME final list, which is exactly the kind of bug that would make
    every "request" in the trace look identical regardless of what actually changed turn
    to turn.

    `extra_kwargs` holds every OTHER keyword the call received (`tool_choice`,
    `temperature`, anything else a real caller might pass), deep-copied the same way. An
    earlier version of this class only kept `model`/`messages`/`tools` -- meaning a field
    like `tool_choice` was discarded here, before the converter ever got a chance to reject
    it, contradicting the bridge's own explicit-rejection contract (a field silently
    dropped in the fake client is indistinguishable from one silently dropped anywhere
    else). Capturing it here and rejecting it in `bridge.convert_captured_calls` is what
    makes that rejection real rather than accidental."""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    extra_kwargs: dict[str, Any]
    response: FakeChatCompletion
    captured_at: float


class _Completions:
    def __init__(self, client: "ScriptedClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> FakeChatCompletion:
        return self._client._create(**kwargs)


class _Chat:
    def __init__(self, client: "ScriptedClient") -> None:
        self.completions = _Completions(client)


class ScriptedClient:
    """Fake `client`: `.chat.completions.create(**kwargs)` matches the real duck-typed
    interface `guarded_tool_loop` calls, but returns pre-scripted responses in order and
    makes no network call. Every call is recorded in `captured` before returning, so a
    test (or the pilot bridge) can convert exactly what was "sent" into a RawTrace."""

    def __init__(self, script: list[FakeChatCompletion]) -> None:
        self.chat = _Chat(self)
        self._script = list(script)
        self.captured: list[CapturedCall] = []

    def load_script(self, script: list[FakeChatCompletion]) -> None:
        """Replace the pending response queue -- e.g. to simulate a second, independent
        loop invocation (a restart) reusing the same client. Does not clear `captured`;
        callers that want a clean capture list for the new invocation should clear it
        themselves."""
        self._script = list(script)

    def _create(self, **kwargs: Any) -> FakeChatCompletion:
        if not self._script:
            raise ScriptExhaustedError(
                f"scripted client asked for a response but the fixture only provided "
                f"{len(self.captured)}"
            )
        response = self._script.pop(0)
        extra = {k: v for k, v in kwargs.items() if k not in ("model", "messages", "tools")}
        self.captured.append(
            CapturedCall(
                model=copy.deepcopy(kwargs.get("model", "")),
                messages=copy.deepcopy(kwargs.get("messages", [])),
                tools=copy.deepcopy(kwargs.get("tools", [])),
                extra_kwargs=copy.deepcopy(extra),
                response=response,
                captured_at=time.time(),
            )
        )
        return response
