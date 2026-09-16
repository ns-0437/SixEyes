"""End-to-end: JsonlSource -> Fingerprint -> Divergence, wired through the real Graph and
Executor -- not just unit-level function calls. This is what actually proves the nodes
compose the way ARCHITECTURE.md's diagram claims they do.
"""

from __future__ import annotations

from pathlib import Path

from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.graph import DiskCache, Executor, Graph
from sixeyes.ingest.jsonl import JsonlSource

FIXTURE = Path(__file__).parent / "fixtures" / "sample_trace.jsonl"


def _pipeline(path: Path) -> Graph:
    graph = Graph("ingest_pipeline")
    graph.add(JsonlSource("source", path=str(path), workload_id="wl_sample"))
    graph.add(Fingerprint("fingerprint"), trace="source")
    graph.add(Divergence("divergence", workload_id="wl_sample"), fingerprints="fingerprint")
    return graph


async def test_full_pipeline_localizes_the_fixtures_injected_timestamp(tmp_path: Path) -> None:
    """The fixture's two requests differ by an embedded timestamp in the system prompt
    (a new user turn was also appended) -- confirm the pipeline reports system_changed,
    not messages_changed, since the earlier segment diverges first."""
    graph = _pipeline(FIXTURE)

    result = await Executor(cache=DiskCache(root=tmp_path / "cache")).run(graph, targets=["divergence"])

    reports = result["divergence"]
    assert len(reports) == 1
    assert reports[0].kind is DivergenceKind.SYSTEM_CHANGED
    assert reports[0].previous_request_id == "req_1"
    assert reports[0].request_id == "req_2"


async def test_pipeline_manifest_shows_only_the_source_as_content_bearing(tmp_path: Path) -> None:
    graph = _pipeline(FIXTURE)
    result = await Executor(cache=DiskCache(root=tmp_path / "cache")).run(graph, targets=["divergence"])

    assert result.manifest is not None
    assert result.manifest.content_bearing_nodes == ("source",)

    # and nothing from the source node landed on disk
    written = list((tmp_path / "cache").rglob("*.pkl"))
    cached_ids = set()
    for f in written:
        cached_ids.add(f.stem)
    # we can't cheaply invert a cache key back to a node id, so assert the weaker but
    # still meaningful property: the number of persisted entries equals the number of
    # non-content-bearing nodes that ran (fingerprint, divergence), not three.
    assert len(written) == 2


async def test_pipeline_is_incremental_when_only_divergence_changes(tmp_path: Path) -> None:
    """Bumping Divergence's version should not force JsonlSource or Fingerprint to
    re-run -- this is the whole commercial point of owning a content-addressed graph."""
    from sixeyes.graph.node import NodeKind, node as node_decorator

    cache = DiskCache(root=tmp_path / "cache")
    calls = {"fingerprint": 0}

    class CountingFingerprint(Fingerprint):
        async def execute(self, ctx, **inputs):  # type: ignore[no-untyped-def]
            calls["fingerprint"] += 1
            return await super().execute(ctx, **inputs)

    graph_v1 = Graph("p1")
    graph_v1.add(JsonlSource("source", path=str(FIXTURE), workload_id="wl_sample"))
    graph_v1.add(CountingFingerprint("fingerprint"), trace="source")
    graph_v1.add(Divergence("divergence", workload_id="wl_sample"), fingerprints="fingerprint")
    await Executor(cache=cache).run(graph_v1, targets=["divergence"])
    assert calls["fingerprint"] == 1

    class DivergenceV2(Divergence):
        version = "2"

    graph_v2 = Graph("p2")
    graph_v2.add(JsonlSource("source", path=str(FIXTURE), workload_id="wl_sample"))
    graph_v2.add(CountingFingerprint("fingerprint"), trace="source")
    graph_v2.add(DivergenceV2("divergence", workload_id="wl_sample"), fingerprints="fingerprint")
    await Executor(cache=cache).run(graph_v2, targets=["divergence"])

    # Fingerprint is content-free and PURE -- its cache key is unaffected by Divergence's
    # version bump, so it must have served from cache the second time, not re-run.
    assert calls["fingerprint"] == 1
