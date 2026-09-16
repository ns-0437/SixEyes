"""Compare consecutive fingerprints -> DivergenceReport.

Mirrors the semantics of Anthropic's cache-diagnosis API (verified against their live docs,
2026-09-16): report the *first* point two requests diverge, expressed as which structural
segment it falls in plus a token offset, entirely from content-free fingerprints. The
difference is scope -- this compares any two providers' traces from an export, not just
consecutive calls to one provider's API with a beta header.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sixeyes.fingerprint.types import (
    DivergenceKind,
    DivergenceReport,
    RequestFingerprint,
    divergence_kind_for_segment,
)
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind


def compare(workload_id: str, previous: RequestFingerprint, current: RequestFingerprint) -> DivergenceReport:
    """Find the first index where the two chains differ.

    If every index up to the shorter chain's length matches, the shorter is a true prefix
    of the longer -- healthy growth (or a harmlessly shorter follow-up), not a divergence.
    Only a mismatch *within* the shared range is a real cache-breaking change.
    """
    shared = min(previous.token_count, current.token_count)
    for i in range(shared):
        if previous.chain[i] != current.chain[i]:
            span = current.segment_at(i)
            return DivergenceReport(
                workload_id=workload_id,
                previous_request_id=previous.request_id,
                request_id=current.request_id,
                kind=divergence_kind_for_segment(span.kind),
                segment_offset=i - span.start,
                combined_offset=i,
                cache_missed_tokens=current.token_count - i,
            )

    return DivergenceReport(
        workload_id=workload_id,
        previous_request_id=previous.request_id,
        request_id=current.request_id,
        kind=DivergenceKind.NONE,
    )


class Divergence(Node):
    """tuple[RequestFingerprint, ...] -> tuple[DivergenceReport, ...], one per consecutive
    pair. The first request in a trace has nothing to compare against and produces no
    report, matching a first API turn having no previous_message_id to diagnose against."""

    kind = NodeKind.PURE
    version = "1"
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
