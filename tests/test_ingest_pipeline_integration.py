"""End-to-end: JsonlSource -> Fingerprint -> Divergence, wired through the real Graph and
Executor -- not just unit-level function calls. This is what actually proves the nodes
compose the way ARCHITECTURE.md's diagram claims they do.
"""

from __future__ import annotations

import json
from pathlib import Path

from sixeyes.core.ids import content_hash
from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.graph import DiskCache, Executor, Graph
from sixeyes.ingest.jsonl import JsonlSource

FIXTURE = Path(__file__).parent / "fixtures" / "sample_trace.jsonl"
TEST_KEY = b"\x03" * 32  # fixed, in-test-only -- never touches the real machine's key file


def _pipeline(path: Path) -> Graph:
    graph = Graph("ingest_pipeline")
    graph.add(JsonlSource("source", path=str(path), workload_id="wl_sample"))
    graph.add(Fingerprint("fingerprint", key=TEST_KEY), trace="source")
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
    assert reports[0].previous_request_ref == content_hash("request_id", "req_1")
    assert reports[0].request_ref == content_hash("request_id", "req_2")


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
    cache = DiskCache(root=tmp_path / "cache")
    calls = {"fingerprint": 0}

    class CountingFingerprint(Fingerprint):
        async def execute(self, ctx, **inputs):  # type: ignore[no-untyped-def]
            calls["fingerprint"] += 1
            return await super().execute(ctx, **inputs)

    graph_v1 = Graph("p1")
    graph_v1.add(JsonlSource("source", path=str(FIXTURE), workload_id="wl_sample"))
    graph_v1.add(CountingFingerprint("fingerprint", key=TEST_KEY), trace="source")
    graph_v1.add(Divergence("divergence", workload_id="wl_sample"), fingerprints="fingerprint")
    await Executor(cache=cache).run(graph_v1, targets=["divergence"])
    assert calls["fingerprint"] == 1

    class DivergenceV2(Divergence):
        version = "3"  # Divergence's own base version is "2" after the 2026-09-16 fixes

    graph_v2 = Graph("p2")
    graph_v2.add(JsonlSource("source", path=str(FIXTURE), workload_id="wl_sample"))
    graph_v2.add(CountingFingerprint("fingerprint", key=TEST_KEY), trace="source")
    graph_v2.add(DivergenceV2("divergence", workload_id="wl_sample"), fingerprints="fingerprint")
    await Executor(cache=cache).run(graph_v2, targets=["divergence"])

    # Fingerprint is content-free and PURE -- its cache key is unaffected by Divergence's
    # version bump, so it must have served from cache the second time, not re-run.
    assert calls["fingerprint"] == 1


async def test_changed_file_with_preserved_mtime_still_invalidates_the_fingerprint(tmp_path: Path) -> None:
    """Regression (independent review, 2026-09-16): JsonlSource's cache key used to be
    derived from the file's mtime as a proxy for content. Editing a file and then
    resetting its mtime (deliberately here, but a coarse filesystem clock or a fast
    successive write can do the same by accident) left the key unchanged, so Fingerprint
    -- keyed off JsonlSource's cache key, not its own content-bearing input -- served a
    stale result computed from the *old* file content, even though JsonlSource itself
    (exempt from persistence entirely, being content-bearing) correctly re-read the new
    bytes. Fixed by keying off a hash of the file's actual bytes instead of mtime."""
    import os

    path = tmp_path / "trace.jsonl"
    row = {"request_id": "r", "timestamp": 1.0, "model": "m", "system": "alpha", "messages": []}
    path.write_text(json.dumps(row), encoding="utf-8")
    original_stat = path.stat()

    graph = Graph("file")
    graph.add(JsonlSource("source", path=str(path)))
    graph.add(Fingerprint("fingerprint", key=TEST_KEY), trace="source")

    executor = Executor(cache=DiskCache(root=tmp_path / "cache"))
    first = await executor.run(graph)

    row["system"] = "bravo"
    path.write_text(json.dumps(row), encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))  # preserve mtime

    second = await executor.run(graph)

    assert second["source"].requests[0].system == "bravo"
    assert first["fingerprint"][0].segment("system").chain != second["fingerprint"][0].segment("system").chain
