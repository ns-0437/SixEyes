"""Transient, content-bearing request/response snapshots for fake and local clients.

These objects must never be logged or persisted; export only a selected pilot report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str
    arguments: str
    """Exactly as a real model would emit it: a raw string, not parsed. May be invalid
    JSON -- see RawToolCall.arguments_raw's docstring for why that must be preserved."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    function: FunctionCall
    type: str = "function"


@dataclass(frozen=True, slots=True)
class Message:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class Choice:
    message: Message


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class Completion:
    choices: tuple[Choice, ...]
    usage: Usage | None = None


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
    response: Completion
    captured_at: float


