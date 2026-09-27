"""Minimal structural report for the pilot, not the Phase 6 economics renderer.

The export deliberately selects counts, fixed enum values and offsets. It does not
serialize raw traces, workload labels, model names, timestamps or graph manifests.
"""

from __future__ import annotations

import secrets

from pilots.agentfuse.bridge import InMemoryTraceSource, convert_captured_calls
from pilots.agentfuse.fake_client import CapturedCall
from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint
from sixeyes.graph import Executor, Graph
from sixeyes.graph.cache import NullCache
from sixeyes.report import StructuralReport as PilotReport

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
