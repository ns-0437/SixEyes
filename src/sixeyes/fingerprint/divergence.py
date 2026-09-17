"""Compare consecutive fingerprints -> DivergenceReport.

Mirrors the semantics of Anthropic's cache-diagnosis API (verified against their live docs,
2026-09-16): report the *first* point two requests diverge, expressed as which structural
segment it falls in plus a unit offset, entirely from content-free fingerprints. The
difference is scope -- this compares any two providers' traces from an export, not just
consecutive calls to one provider's API with a beta header.

This is a structural diff, not an observed-cache-miss or a dollar figure: see
fingerprint.types.DivergenceReport's docstring for exactly what it does and does not
prove, and CLAUDE.md rule 1 for the confidence-tier vocabulary that governs how a
downstream Finding may present a number derived from this.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sixeyes.fingerprint.types import (
    GROWTH_TOLERANT_SEGMENTS,
    SEGMENT_ORDER,
    DivergenceKind,
    DivergenceReport,
    IncomparableFingerprintsError,
    RequestFingerprint,
    SegmentFingerprint,
    divergence_kind_for_segment,
)
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind


def _segment_divergence_offset(
    previous: SegmentFingerprint, current: SegmentFingerprint, *, tolerate_growth: bool
) -> int | None:
    """The unit offset of the first divergence within one segment, or None if `current` is
    exactly equal to `previous`, or -- only when `tolerate_growth` is True, i.e. only for
    `messages` (see GROWTH_TOLERANT_SEGMENTS) -- a genuine append-only extension of it.

    A *shrink* with a fully matching shared prefix is deliberately NOT treated as safe: an
    earlier version of this function returned None here, calling a matching truncation
    universally harmless. An independent review correctly identified that as an unproven
    claim -- this analyzer has no visibility into where the customer's own cache_control
    breakpoint sits, so it cannot know whether a shortened segment still hits it. Reporting
    the truncation point, rather than staying silent, is the honest behaviour. The same
    logic now applies to *growth* in a non-growth-tolerant segment: appending new system
    instructions or adding a tool is reported, not silently treated as safe.
    """
    shared = min(previous.unit_count, current.unit_count)
    for i in range(shared):
        if previous.chain[i] != current.chain[i]:
            return i
    if current.unit_count == previous.unit_count:
        return None  # identical
    if tolerate_growth and current.unit_count > previous.unit_count:
        return None  # genuine append-only extension -- messages only
    return shared  # a shrink, or growth in a segment with no evidence that's safe


def compare(workload_id: str, previous: RequestFingerprint, current: RequestFingerprint) -> DivergenceReport:
    """Compare two consecutive fingerprints in the same session.

    Raises `IncomparableFingerprintsError` if the two fingerprints were produced under
    different local keys (`key_ref` differs) -- their chains are not meaningfully
    comparable at all in that case, and reporting a divergence would misrepresent a key
    rotation as a customer-content change.

    Model identity is checked first and independently of prefix order: per Anthropic's
    documented behaviour, a model change invalidates cache compatibility outright rather
    than occupying a position within the tools/system/messages prefix. Only if the model
    matches do we walk SEGMENT_ORDER looking for the first segment that diverged --
    segments are each hashed independently (fingerprint.fingerprint), so a segment's own
    divergence can never be caused by, or attributed to, a change in a different segment.
    """
    if previous.key_ref != current.key_ref:
        raise IncomparableFingerprintsError(
            "cannot compare fingerprints produced under different local keys "
            f"({previous.key_ref!r} vs {current.key_ref!r}) -- rotate keys deliberately, "
            "not silently across a comparison"
        )

    if previous.model_digest != current.model_digest:
        return DivergenceReport(
            workload_id=workload_id,
            previous_request_ref=previous.request_ref,
            request_ref=current.request_ref,
            kind=DivergenceKind.MODEL_CHANGED,
            segment_offset=0,
            cache_missed_units=current.total_units,
        )

    if previous.tool_choice_digest != current.tool_choice_digest:
        # Request control, not an ordered prefix segment. No provider-token position
        # or affected-unit estimate can be inferred for this change.
        return DivergenceReport(
            workload_id=workload_id,
            previous_request_ref=previous.request_ref,
            request_ref=current.request_ref,
            kind=DivergenceKind.TOOL_CHOICE_CHANGED,
        )

    remaining_units = current.total_units
    for kind in SEGMENT_ORDER:
        prev_seg = previous.segment(kind)
        curr_seg = current.segment(kind)
        offset = _segment_divergence_offset(
            prev_seg, curr_seg, tolerate_growth=kind in GROWTH_TOLERANT_SEGMENTS
        )
        if offset is not None:
            cache_missed = (curr_seg.unit_count - offset) + (remaining_units - curr_seg.unit_count)
            return DivergenceReport(
                workload_id=workload_id,
                previous_request_ref=previous.request_ref,
                request_ref=current.request_ref,
                kind=divergence_kind_for_segment(kind),
                segment_offset=offset,
                cache_missed_units=cache_missed,
            )
        remaining_units -= curr_seg.unit_count

    return DivergenceReport(
        workload_id=workload_id,
        previous_request_ref=previous.request_ref,
        request_ref=current.request_ref,
        kind=DivergenceKind.NONE,
    )


class Divergence(Node):
    """tuple[RequestFingerprint, ...] -> tuple[DivergenceReport, ...], one per consecutive
    pair. The first request in a trace has nothing to compare against and produces no
    report, matching a first API turn having no previous_message_id to diagnose against."""

    kind = NodeKind.PURE
    version = "4"
    inputs: ClassVar[dict[str, type]] = {"fingerprints": tuple}
    output = tuple
    content_bearing = False

    async def execute(self, ctx: RunContext, **inputs: Any) -> tuple[DivergenceReport, ...]:
        fingerprints: tuple[RequestFingerprint, ...] = inputs["fingerprints"]
        workload_id = self.config.get("workload_id", "")
        reports = tuple(
            compare(workload_id, fingerprints[i - 1], fingerprints[i])
            for i in range(1, len(fingerprints))
        )
        broke = sum(1 for r in reports if r.kind is not DivergenceKind.NONE)
        ctx.log("compared %d consecutive pairs, %d diverged", len(reports), broke)
        return reports
