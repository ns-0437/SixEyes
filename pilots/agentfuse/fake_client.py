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
from typing import Any

__all__ = [
    "CapturedCall", "FakeFunction", "FakeToolCall", "FakeMessage", "FakeChoice",
    "FakeUsage", "FakeChatCompletion", "ScriptedClient", "ScriptExhaustedError",
]


class ScriptExhaustedError(RuntimeError):
    """The scripted loop asked for more responses than the fixture provided -- a fixture
    bug, not something to paper over by looping the last response silently."""


from pilots.agentfuse.capture import (
    CapturedCall as CapturedCall,
    FunctionCall as FakeFunction,
    ToolCall as FakeToolCall,
    Message as FakeMessage,
    Choice as FakeChoice,
    Usage as FakeUsage,
    Completion as FakeChatCompletion,
)


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
