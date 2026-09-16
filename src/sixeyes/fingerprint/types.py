"""Content-free fingerprints and divergence reports.

Everything here is safe to persist, log, and emit in a report: every field is a hash,
a count, or an offset. This is the boundary CLAUDE.md rule 3 draws -- nothing upstream of
this module (see sixeyes.ingest.types) may cross it; everything at or downstream of it may.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from sixeyes.core.ids import Digest

SegmentKind = str  # "model" | "system" | "tools" | "messages" -- see fingerprint.fingerprint


@dataclass(frozen=True, slots=True)
class SegmentSpan:
    """Where one structural segment lives within a request's combined token chain.

    Spans are contiguous and ordered (model, system, tools, messages) because that's the
    order a provider concatenates a request into a single prefix for caching purposes --
    a divergence anywhere invalidates everything after it, which is exactly what walking
    the combined chain index-by-index captures.
    """

    kind: SegmentKind
    start: int
    length: int

    def content_key(self) -> Any:
        return [self.kind, self.start, self.length]


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    """A content-free fingerprint of one request, safe to cache, log, and compare."""

    request_id: str
    timestamp: float
    token_count: int
    chain: tuple[Digest, ...]
    """Rolling hash chain, one entry per token: chain[i] = H(chain[i-1], token_i). Two
    fingerprints share a common prefix of exactly the length of their longest matching
    chain prefix -- this is what makes divergence localization exact rather than
    approximate."""
    spans: tuple[SegmentSpan, ...]

    def content_key(self) -> Any:
        return [self.request_id, self.timestamp, self.token_count, list(self.chain),
                list(self.spans)]

    def segment_at(self, index: int) -> SegmentSpan:
        for span in self.spans:
            if span.start <= index < span.start + span.length:
                return span
        raise IndexError(f"token index {index} is outside all segments (0..{self.token_count})")


class DivergenceKind(str, Enum):
    """Named the same way as Anthropic's own `cache_miss_reason.type` (verified against
    their live cache-diagnosis docs, 2026-09-16) so a report reads the same regardless of
    whether the underlying detection came from a provider's native API or from a trace
    export analyzed after the fact."""

    NONE = "none"
    MODEL_CHANGED = "model_changed"
    SYSTEM_CHANGED = "system_changed"
    TOOLS_CHANGED = "tools_changed"
    MESSAGES_CHANGED = "messages_changed"


_KIND_BY_SEGMENT = {
    "model": DivergenceKind.MODEL_CHANGED,
    "system": DivergenceKind.SYSTEM_CHANGED,
    "tools": DivergenceKind.TOOLS_CHANGED,
    "messages": DivergenceKind.MESSAGES_CHANGED,
}


def divergence_kind_for_segment(segment_kind: SegmentKind) -> DivergenceKind:
    return _KIND_BY_SEGMENT[segment_kind]


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """The result of comparing two consecutive requests' fingerprints.

    `kind is DivergenceKind.NONE` means the shorter chain is a true prefix of the longer
    one -- healthy conversation growth (or a harmlessly shorter follow-up), not a cache
    problem. Anything else means some token inside the shared range actually changed.
    """

    workload_id: str
    previous_request_id: str
    request_id: str
    kind: DivergenceKind
    segment_offset: int = 0
    """Token offset within the diverging segment (0 when kind is NONE)."""
    combined_offset: int = 0
    """Token offset within the full combined chain."""
    cache_missed_tokens: int = 0
    """Tokens in the *new* request from combined_offset onward -- an estimate of how much
    cacheable prefix was lost, mirroring Anthropic's cache_missed_input_tokens semantics."""

    def content_key(self) -> Any:
        return [self.workload_id, self.previous_request_id, self.request_id, self.kind.value,
                self.segment_offset, self.combined_offset, self.cache_missed_tokens]
