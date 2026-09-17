"""Minimal structural report for the pilot, not the Phase 6 economics renderer.

The export deliberately selects counts, fixed enum values and offsets. It does not
serialize raw traces, workload labels, model names, timestamps or graph manifests.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from pilots.agentfuse.bridge import InMemoryTraceSource, convert_captured_calls
from pilots.agentfuse.fake_client import CapturedCall
from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint
from sixeyes.fingerprint.types import DivergenceKind, DivergenceReport
from sixeyes.graph import Executor, Graph
from sixeyes.graph.cache import NullCache

_NEXT_CHECK = {
    DivergenceKind.NONE: "No structural change detected; verify actual provider cache usage separately.",
    DivergenceKind.MODEL_CHANGED: "Check whether the model change was intentional.",
    DivergenceKind.TOOL_CHOICE_CHANGED: "Check whether tool-selection policy changed intentionally.",
    DivergenceKind.TOOLS_CHANGED: "Inspect tool ordering and definitions at this request boundary.",
    DivergenceKind.SYSTEM_CHANGED: "Inspect system prompt construction for dynamic or reordered content.",
    DivergenceKind.MESSAGES_CHANGED: "Inspect edits, result bindings, or restart truncation in prior messages.",
}


@dataclass(frozen=True)
class PilotReport:
    request_count: int
    comparisons: tuple[DivergenceReport, ...]

    def to_dict(self) -> dict[str, Any]:
        rows = []
        for index, report in enumerate(self.comparisons, start=2):
            has_offset = report.kind in {
                DivergenceKind.TOOLS_CHANGED, DivergenceKind.SYSTEM_CHANGED,
                DivergenceKind.MESSAGES_CHANGED,
            }
            rows.append({
                "previous_request_number": index - 1,
                "request_number": index,
                "change": report.kind.value,
                "segment_unit_offset": report.segment_offset if has_offset else None,
                "next_check": _NEXT_CHECK[report.kind],
            })
        return {
            "schema_version": 1,
            "scope": "structural_comparison_only",
            "request_count": self.request_count,
            "comparison_count": len(rows),
            "changed_pair_count": sum(r.kind is not DivergenceKind.NONE for r in self.comparisons),
            "comparisons": rows,
            "limitations": [
                "No observed cache hits, measured savings, or redundant-tool-call diagnosis.",
                "Offsets are structural units, not provider tokens or exact nested fields.",
                "Compare requests within one logical session only; intentional restarts may be flagged.",
            ],
        }

    def render_text(self) -> str:
        data = self.to_dict()
        lines = [
            "SixEyes structural pilot report",
            f"Requests: {self.request_count}; compared pairs: {len(self.comparisons)}; "
            f"changed pairs: {data['changed_pair_count']}",
        ]
        if not self.comparisons:
            lines.append("Insufficient requests for comparison; at least two are needed.")
        for row in data["comparisons"]:
            offset = row["segment_unit_offset"]
            location = f" (unit offset {offset})" if offset is not None else ""
            lines.append(
                f"Request {row['previous_request_number']} -> {row['request_number']}: "
                f"{row['change']}{location}. {row['next_check']}"
            )
        lines.extend(data["limitations"])
        return "\n".join(lines)


async def analyze_captured_calls(captured: list[CapturedCall]) -> PilotReport:
    """One local session, ephemeral HMAC key, no persistent cache or raw-data export.

    The caller owns and retains `captured`; this function does not erase its objects
    or claim secure memory erasure. Only the selected structural report is returned.
    """
    trace = convert_captured_calls(captured, workload_id="pilot")
    graph = Graph("pilot")
    graph.add(InMemoryTraceSource("source", trace=trace))
    graph.add(Fingerprint("fingerprint", key=secrets.token_bytes(32)), trace="source")
    graph.add(Divergence("divergence", workload_id="pilot"), fingerprints="fingerprint")
    result = await Executor(cache=NullCache()).run(graph, targets=["divergence"])
    return PilotReport(len(trace.requests), result["divergence"])
