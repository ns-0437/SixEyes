"""End-to-end: JsonlSource -> Fingerprint -> Divergence, wired through the real Graph and
Executor -- not just unit-level function calls. This is what actually proves the nodes
compose the way ARCHITECTURE.md's diagram claims they do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint, _keyed_request_ref, fingerprint_request
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
    assert reports[0].previous_request_ref == _keyed_request_ref(TEST_KEY, "req_1")
    assert reports[0].request_ref == _keyed_request_ref(TEST_KEY, "req_2")


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
        version = "4"  # Divergence's own base version is "3" after the 2026-09-16 fixes

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


async def test_source_key_and_parse_use_the_same_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    """Regression (independent follow-up review, 2026-09-16): config_key() and execute()
    used to each read the file independently. An external edit landing between those two
    reads let the cache-key digest (from read #1) and the actually-parsed content (from
    read #2) describe different bytes -- a downstream node's cache entry ended up bound to
    content it never processed. Fixed by sharing exactly one read between the two (see
    JsonlSource._read_snapshot); this simulates that edit deterministically rather than
    relying on race timing."""
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m", "system": "alpha", "messages": []}),
        encoding="utf-8",
    )

    original_execute = JsonlSource.execute
    state = {"first": True}

    async def execute_with_external_edit(self: JsonlSource, ctx: Any, **kwargs: Any) -> Any:
        if state["first"]:
            state["first"] = False
            path.write_text(
                json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m",
                            "system": "bravo", "messages": []}),
                encoding="utf-8",
            )
        return await original_execute(self, ctx, **kwargs)

    monkeypatch.setattr(JsonlSource, "execute", execute_with_external_edit)

    graph = Graph("snapshot")
    graph.add(JsonlSource("source", path=str(path)))
    graph.add(Fingerprint("fingerprint", key=TEST_KEY), trace="source")

    executor = Executor(cache=DiskCache(root=tmp_path / "cache"))
    await executor.run(graph)  # first run: the external edit fires during execute()

    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m", "system": "alpha", "messages": []}),
        encoding="utf-8",
    )
    second = await executor.run(graph)  # second run: clean, no injected edit

    assert second["source"].requests[0].system == "alpha"
    expected = fingerprint_request(second["source"].requests[0], TEST_KEY)
    assert second["fingerprint"][0].segments == expected.segments


async def test_missing_file_after_a_successful_run_fails_the_next_run_instead_of_replaying(
    tmp_path: Path,
) -> None:
    """Regression (independent third-round review, 2026-09-16): a successful run followed
    by deleting the input file and running again used to succeed, silently returning the
    first run's trace -- the forced snapshot refresh's read raised before the old
    attribute was ever cleared, so a missing file left the previous run's bytes looking
    current. Fixed via RunScoped, which invalidates before attempting the new read, not
    after. A missing input must now fail the run, not replay stale data."""
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m", "system": "alpha", "messages": []}),
        encoding="utf-8",
    )

    graph = Graph("missing_file")
    graph.add(JsonlSource("source", path=str(path)))

    executor = Executor(cache=DiskCache(root=tmp_path / "cache"))
    first = await executor.run(graph)
    assert first["source"].requests[0].system == "alpha"

    path.unlink()

    with pytest.raises(Exception):
        await executor.run(graph)

    # restoring the file with *different* content must be processed fresh, not skipped
    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m", "system": "gamma", "messages": []}),
        encoding="utf-8",
    )
    third = await executor.run(graph)
    assert third["source"].requests[0].system == "gamma"


async def test_default_key_rotating_mid_run_does_not_poison_the_cache(tmp_path: Path, monkeypatch: Any) -> None:
    """Regression (independent third-round review, 2026-09-16): Fingerprint resolved its
    default local key separately in config_key() and execute(). A key rotated in between
    those two calls (simulated here without touching any real key file) left the node's
    cache identity computed under key A but its actual fingerprint output computed under
    key B -- that run's fingerprints were then stored under A's cache slot. A later run
    planned under A again got a cache hit and silently served back B's mismatched result.
    Fixed by resolving the key once per run via RunScoped, shared between the two calls."""
    import sixeyes.fingerprint.fingerprint as fingerprint_module

    key_a = b"\x01" * 32
    key_b = b"\x02" * 32
    state = {"key": key_a, "rotate": True}
    monkeypatch.setattr(fingerprint_module, "load_or_create_key", lambda: state["key"])

    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m", "system": "alpha", "messages": []}),
        encoding="utf-8",
    )

    original_execute = JsonlSource.execute

    async def rotate_after_planning(self: JsonlSource, ctx: Any, **kwargs: Any) -> Any:
        if state["rotate"]:
            state["rotate"] = False
            state["key"] = key_b  # simulates rotation happening between config_key() and execute()
        return await original_execute(self, ctx, **kwargs)

    monkeypatch.setattr(JsonlSource, "execute", rotate_after_planning)

    graph = Graph("key_rotation")
    graph.add(JsonlSource("source", path=str(path)))
    graph.add(Fingerprint("fingerprint"), trace="source")

    executor = Executor(cache=DiskCache(root=tmp_path / "cache"))
    await executor.run(graph)  # plans under A, rotates to B mid-run

    state["key"] = key_a  # rotation reverted -- planning under A again
    second = await executor.run(graph)

    expected = fingerprint_request(second["source"].requests[0], key_a)
    assert second["fingerprint"][0].key_ref == expected.key_ref
    assert second["fingerprint"][0].segments == expected.segments
