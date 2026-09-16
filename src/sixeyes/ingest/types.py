"""Provider-agnostic trace model.

Everything in this module holds raw customer content -- prompt text, tool schemas, message
history -- for the duration of a run. Per CLAUDE.md rule 3, nothing built from these types
may cross the collector boundary (be written to a persistent cache, logged, or emitted in a
report). Only `fingerprint.types` output, built *from* these, is safe to persist.

Any node whose output includes one of these types must declare `content_bearing = True`
(see graph/node.py) -- the executor enforces the persistence boundary from there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class RawToolDef:
    """A tool/function definition as sent to the model."""

    name: str
    description: str
    schema_json: str
    """Canonical JSON (sort_keys=True) of the tool's parameter schema. Ingest adapters are
    responsible for producing this canonically -- see `canonical_json` -- so that two
    semantically identical schemas serialized in different key orders fingerprint the same
    way, and a genuine reordering of *tools* (not of a schema's keys) is what shows up as
    drift."""

    def content_key(self) -> Any:
        _refuse("RawToolDef")


@dataclass(frozen=True, slots=True)
class RawMessage:
    """One turn in the conversation."""

    role: Role
    content: str
    tool_call_id: str | None = None

    def content_key(self) -> Any:
        _refuse("RawMessage")


@dataclass(frozen=True, slots=True)
class RawRequest:
    """One model call, normalized from whatever trace format it arrived in.

    `model` is a cache-*compatibility* dimension, not part of the ordered prefix: per
    Anthropic's documented behaviour (verified 2026-09-16), the prefix a provider
    concatenates for caching purposes is `tools, system, messages`, in that order --
    `fingerprint.fingerprint` walks segments in that same order and checks `model`
    separately and first. An earlier version of this docstring said `model, system, tools,
    messages`, which was simply wrong; a 2026-09-16 review caught it.
    """

    request_id: str
    timestamp: float
    model: str
    system: str | None
    tools: tuple[RawToolDef, ...]
    messages: tuple[RawMessage, ...]
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_cache_read_tokens: int | None = None

    def content_key(self) -> Any:
        _refuse("RawRequest")


@dataclass(frozen=True, slots=True)
class RawTrace:
    """A sequence of requests for one workload, in the order they were made.

    Consecutive requests are assumed to belong to the same logical conversation/session
    for divergence comparison -- see `fingerprint.divergence`. A multi-session trace should
    be split into one RawTrace per session before fingerprinting; that grouping is a
    near-term ingest concern, not handled here yet.
    """

    workload_id: str
    requests: tuple[RawRequest, ...]

    def content_key(self) -> Any:
        _refuse("RawTrace")


def _refuse(type_name: str) -> Any:
    raise TypeError(
        f"{type_name} holds raw customer content and must never be content-hashed or "
        f"written to a persistent cache directly -- fingerprint it first "
        f"(see sixeyes.fingerprint.fingerprint)."
    )
