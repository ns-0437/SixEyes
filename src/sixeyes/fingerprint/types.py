"""Content-free fingerprints and divergence reports.

Everything here is safe to persist, log, and emit in a report: every field is a hash, a
count, or an offset. This is the boundary CLAUDE.md rule 3 draws -- nothing upstream of
this module (see sixeyes.ingest.types) may cross it; everything at or downstream of it may.

Corrections from the original design, all driven by independent review verified against
primary sources and reproduced locally (2026-09-16, in two rounds):

1. Anthropic's documented cache-prefix order is ``tools, system, messages`` -- model is a
   separate cache-*compatibility* dimension (different models can't share a cache entry at
   all), not a token at position zero inside the prefix. `RequestFingerprint.model_digest`
   is compared first and independently; `segments` covers only tools/system/messages, in
   that order.
2. Segments are hashed *independently* (each with its own seed), not as one continuous
   chain. A continuous chain meant deleting an earlier segment (e.g. the whole system
   prompt) shifted every later segment's absolute position, and the comparison attributed
   the resulting mismatch to whichever segment happened to occupy that shifted index --
   typically `messages`, even when the actual cause was the system prompt disappearing.
   Independent per-segment chains can't suffer that misattribution: removing `system`
   changes only `system`'s own chain, never `messages`'s.
3. Append-only growth tolerance applies only to `messages` (see `GROWTH_TOLERANT_SEGMENTS`)
   -- a follow-up review found that appending new text to `system` or adding a `tool`
   silently reported no change, because the first fix pass applied messages' legitimate
   growth tolerance to every segment uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from sixeyes.core.ids import Digest

SegmentKind = str  # "tools" | "system" | "messages" -- see fingerprint.fingerprint


class IncomparableFingerprintsError(ValueError):
    """Raised by `fingerprint.divergence.compare` when two fingerprints were produced
    under different local keys. Their chains are not meaningfully comparable -- every
    step would differ regardless of whether the underlying request changed at all -- so
    reporting a divergence in that situation would misrepresent a key rotation as a
    customer-content change. Fail closed and say so, rather than guess."""

SEGMENT_ORDER: tuple[SegmentKind, ...] = ("tools", "system", "messages")
"""Anthropic's documented prompt-caching prefix order (verified against their live docs,
2026-09-16): https://platform.claude.com/docs/en/build-with-claude/prompt-caching --
"Cache prefixes are created in the following order: tools, system, then messages." A
divergence in an earlier segment here is reported in preference to one in a later segment,
because that's the order a real cache-breaking change actually happens in."""

GROWTH_TOLERANT_SEGMENTS: frozenset[SegmentKind] = frozenset({"messages"})
"""Only `messages` gets append-only growth treated as DivergenceKind.NONE. A 2026-09-16
follow-up review correctly identified that the original code applied that tolerance to
every segment uniformly -- so appending new instructions to `system`, or adding a `tool`,
silently reported no change. `messages` has real evidentiary support for the tolerance
(Anthropic explicitly documents that a growing conversation history keeps the cache warm);
`tools` and `system` do not -- they are typically static configuration, and an append
there is exactly the kind of change a customer needs surfaced, not hidden. Any difference
in a non-growth-tolerant segment -- including a pure append -- is reported."""


@dataclass(frozen=True, slots=True)
class SegmentFingerprint:
    """A content-free, independently-seeded fingerprint of one structural segment."""

    kind: SegmentKind
    unit_count: int
    chain: tuple[Digest, ...]
    """Keyed rolling hash chain (see fingerprint.keys / fingerprint.fingerprint), one
    entry per whitespace-preserving unit -- see fingerprint.tokenize for exactly what a
    "unit" is. This is *not* a provider/BPE token count; do not present it as one."""

    def content_key(self) -> Any:
        return [self.kind, self.unit_count, list(self.chain)]


@dataclass(frozen=True, slots=True)
class RequestFingerprint:
    """A content-free fingerprint of one request, safe to cache, log, and compare.

    `request_ref` is a *keyed* one-way reference to the customer-supplied request id, not
    the id itself, and not merely an unkeyed hash of it -- an arbitrary customer-controlled
    string field is not safe to assume is non-sensitive metadata (a request id is exactly
    the kind of field someone might, by mistake or by convention, stuff a real identifier
    into), and an *unkeyed* digest of a low-entropy identifier is dictionary-guessable the
    same way an unkeyed chain step was (a 2026-09-16 follow-up review demonstrated exactly
    that against the first, unkeyed version of this field).

    `key_ref` is a content-free identifier of *which local key* produced this fingerprint
    -- never the key itself, safe to compare and to put in a cache key, since it's derived
    from 256 bits of local randomness rather than customer-supplied low-entropy content.
    Two fingerprints computed under different keys are not meaningfully comparable (their
    chains would differ regardless of whether the underlying request changed at all) --
    see `fingerprint.divergence.compare`, which checks this before anything else.
    """

    request_ref: Digest
    key_ref: Digest
    timestamp: float
    model_digest: Digest
    """Content-free digest of the model identifier. Compared for exact equality only --
    a model change invalidates cache compatibility entirely, independent of prefix order."""
    segments: tuple[SegmentFingerprint, ...]
    """Exactly one entry per kind in SEGMENT_ORDER, in that order."""
    tool_choice_digest: Digest | None = None
    """Keyed digest of an explicit tool choice; None means omitted. Separate from
    ordered prefix segments: its provider cache effects are not inferred here."""

    def content_key(self) -> Any:
        return [self.request_ref, self.key_ref, self.timestamp, self.model_digest,
                list(self.segments), self.tool_choice_digest]

    def segment(self, kind: SegmentKind) -> SegmentFingerprint:
        for seg in self.segments:
            if seg.kind == kind:
                return seg
        raise KeyError(f"no segment of kind {kind!r}")

    @property
    def total_units(self) -> int:
        return sum(seg.unit_count for seg in self.segments)


class DivergenceKind(str, Enum):
    """Structural comparison categories, not observed provider cache-miss reasons.

    Model/segment names follow the original Anthropic-inspired vocabulary.
    TOOL_CHOICE_CHANGED is SixEyes's separate request-control observation.
    """

    NONE = "none"
    MODEL_CHANGED = "model_changed"
    TOOL_CHOICE_CHANGED = "tool_choice_changed"
    SYSTEM_CHANGED = "system_changed"
    TOOLS_CHANGED = "tools_changed"
    MESSAGES_CHANGED = "messages_changed"


_KIND_BY_SEGMENT = {
    "tools": DivergenceKind.TOOLS_CHANGED,
    "system": DivergenceKind.SYSTEM_CHANGED,
    "messages": DivergenceKind.MESSAGES_CHANGED,
}


def divergence_kind_for_segment(segment_kind: SegmentKind) -> DivergenceKind:
    return _KIND_BY_SEGMENT[segment_kind]


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """The result of comparing two consecutive requests' fingerprints.

    `kind is DivergenceKind.NONE` means model and tool choice match, and every segment's chain is either identical or a
    genuine append-only extension of the previous one. Anything else means some segment
    changed in a way this analyzer cannot prove is safe -- including a truncation with a
    matching shared prefix: the evidence available here cannot establish that a shortened
    segment still hits whatever cache breakpoint the customer's own code placed, so it is
    reported, not silently treated as harmless (a prior version of this type made that
    stronger claim; an independent review correctly identified it as unproven).

    This report is a *structural divergence*, not a proof of wasted spend: a changed
    request may still reuse an earlier cache entry the fingerprint has no visibility into
    (differently-scoped cache_control breakpoints, provider-side eviction, etc.), and an
    *unchanged* request can still miss if the provider's cache entry simply expired.
    Combining this with actual observed usage (RawRequest.usage_*) and a dated price table
    is a separate, later step -- see core.types.Confidence and CLAUDE.md rule 1.
    """

    workload_id: str
    previous_request_ref: Digest
    request_ref: Digest
    kind: DivergenceKind
    segment_offset: int = 0
    """Unit offset within a diverging segment. Zero is a sentinel for NONE,
    MODEL_CHANGED and TOOL_CHOICE_CHANGED; the pilot renderer exports null instead."""
    cache_missed_units: int = 0
    """Units in the *new* request, from the divergence point onward, across the
    diverging segment and everything after it in SEGMENT_ORDER -- an estimate of how much
    cacheable prefix was lost, mirroring the shape of Anthropic's own
    cache_missed_input_tokens, in whitespace units rather than provider tokens. Anthropic
    documents its own estimate as unsuitable for billing; treat this one the same way --
    it is Confidence.DERIVED at best, never a measured dollar figure on its own."""

    def content_key(self) -> Any:
        return [self.workload_id, self.previous_request_ref, self.request_ref, self.kind.value,
                self.segment_offset, self.cache_missed_units]
