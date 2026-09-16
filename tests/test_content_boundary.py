"""Content-boundary enforcement: the runtime half of CLAUDE.md rule 3.

Mirrors test_taint.py's pattern for rule 2, applied to content-bearing nodes: a node
marked content_bearing=True must never have its output land in a persistent cache, even
by accident three refactors from now. These tests attack that guarantee directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from sixeyes.graph import DiskCache, Executor, Graph, MemoryCache, NullCache, node
from tests.conftest import double


def _content_source(node_id: str, value: str = "SECRET-PAYLOAD") -> type:
    @node(output=str, content_bearing=True)
    async def source(ctx: Any) -> str:
        return value

    return source(node_id)


async def test_content_bearing_node_is_not_written_to_disk_cache(tmp_path: Any) -> None:
    cache = DiskCache(root=tmp_path / "cache")
    graph = Graph("t")
    graph.add(_content_source("raw"))

    await Executor(cache=cache).run(graph, targets=["raw"])

    # DiskCache.has() checks its own in-process memo too -- inspect the actual directory,
    # since the claim under test is specifically "nothing was written to disk."
    written_files = list((tmp_path / "cache").rglob("*.pkl"))
    assert written_files == [], f"content-bearing node output was written to disk: {written_files}"


async def test_content_bearing_node_recomputes_every_run_under_a_persistent_cache(tmp_path: Any) -> None:
    """Since it is never cached, calling it twice with a persistent cache configured must
    execute it twice, not silently serve a stale (or worse, disk-backed) copy."""
    calls: list[int] = []

    @node(output=int, content_bearing=True)
    async def counting_source(ctx: Any) -> int:
        calls.append(1)
        return len(calls)

    cache = DiskCache(root=tmp_path / "cache")
    graph = Graph("t")
    graph.add(counting_source("raw"))

    executor = Executor(cache=cache)
    r1 = await executor.run(graph, targets=["raw"])
    r2 = await executor.run(graph, targets=["raw"])

    assert r1["raw"] == 1
    assert r2["raw"] == 2
    assert len(calls) == 2


async def test_content_bearing_node_may_still_use_a_non_persistent_cache(tmp_path: Any) -> None:
    """MemoryCache dies with the process, so passing content through it within a single
    run is safe -- the boundary is about persistence, not caching per se."""
    calls: list[int] = []

    @node(output=int, content_bearing=True)
    async def counting_source(ctx: Any) -> int:
        calls.append(1)
        return len(calls)

    graph = Graph("t")
    src = graph.add(counting_source("raw"))
    graph.add(double("consumer_a"), x="raw")

    # Run twice against the SAME MemoryCache instance within one process: a content-bearing
    # node is still eligible for the ordinary within-run cache-hit path there.
    cache = MemoryCache()
    executor = Executor(cache=cache)
    await executor.run(graph, targets=["raw"])
    await executor.run(graph, targets=["raw"])

    assert len(calls) == 1  # second run was a cache hit, not a recompute


async def test_downstream_content_free_node_is_cacheable_normally(tmp_path: Any) -> None:
    """Only the content-bearing node itself is exempted from persistence -- a downstream
    node whose own output is content-free caches normally, even under DiskCache."""
    calls: list[int] = []

    @node(output=int, declassifies=True)
    async def content_free_consumer(ctx: Any, x: str) -> int:
        calls.append(1)
        return len(x)

    cache = DiskCache(root=tmp_path / "cache")
    graph = Graph("t")
    graph.add(_content_source("raw"))
    graph.add(content_free_consumer("consumer"), x="raw")

    executor = Executor(cache=cache)
    await executor.run(graph, targets=["consumer"])
    await executor.run(graph, targets=["consumer"])

    # the content-bearing source re-ran both times, but its DECLASSIFYING content-free
    # consumer was only actually executed once -- proving persistence resumes once a node
    # explicitly, reviewedly asserts its own output is content-free.
    assert len(calls) == 1


async def test_plain_downstream_node_inherits_content_bearing_by_default(tmp_path: Any) -> None:
    """The fix for a real regression: an ordinary node with no declaration at all,
    consuming content-bearing input, previously defaulted to content_bearing=False and
    was written straight to disk -- a passthrough could persist raw content just by
    existing. Content sensitivity now propagates downstream by default; only an explicit
    `declassifies = True` (test above) stops it."""

    @node(output=int)
    async def plain_consumer(ctx: Any, x: str) -> int:
        return len(x)

    cache = DiskCache(root=tmp_path / "cache")
    graph = Graph("t")
    graph.add(_content_source("raw"))
    graph.add(plain_consumer("consumer"), x="raw")

    result = await Executor(cache=cache).run(graph, targets=["consumer"])
    assert result.manifest is not None
    assert set(result.manifest.content_bearing_nodes) == {"raw", "consumer"}

    written = list((tmp_path / "cache").rglob("*.pkl"))
    assert not written, "an undeclared downstream node persisted content-bearing output"


async def test_manifest_records_which_nodes_were_content_bearing(tmp_path: Any) -> None:
    @node(output=int, declassifies=True)
    async def measure(ctx: Any, x: str) -> int:
        return len(x)

    cache = DiskCache(root=tmp_path / "cache")
    graph = Graph("t")
    graph.add(_content_source("raw"))
    graph.add(measure("after"), x="raw")

    result = await Executor(cache=cache).run(graph, targets=["after"])
    assert result.manifest is not None
    assert result.manifest.content_bearing_nodes == ("raw",)
    assert all(r.outcome == "executed" for r in result.manifest.records)


def test_null_cache_and_memory_cache_declare_non_persistent() -> None:
    assert NullCache.persistent is False
    assert MemoryCache().persistent is False


def test_disk_cache_declares_persistent(tmp_path: Any) -> None:
    assert DiskCache(root=tmp_path / "cache").persistent is True


def test_unrecognised_custom_cache_defaults_to_persistent_fail_closed() -> None:
    """Regression (independent review, 2026-09-16): the executor used
    `getattr(self.cache, "persistent", False)` -- a custom cache that forgot to declare
    `persistent` was treated as *safe* to write content-bearing output to, purely by
    omission. Fixed by flipping the fallback to True: an unrecognised cache is now assumed
    persistent (unsafe) until it proves otherwise, matching the fail-closed default
    `content_bearing` itself already used."""

    class MysteryCache:
        """Deliberately omits `persistent` -- simulates a third-party cache implementation
        that hasn't been updated to declare it."""

        def __init__(self) -> None:
            self.store: dict[Any, Any] = {}

        def get(self, key: Any) -> Any:
            return self.store.get(key, object())  # never a real hit for this test

        def put(self, key: Any, value: Any) -> None:
            self.store[key] = value

        def has(self, key: Any) -> bool:
            return key in self.store

    cache = MysteryCache()
    graph = Graph("t")
    graph.add(_content_source("raw"))

    import asyncio

    asyncio.run(Executor(cache=cache).run(graph, targets=["raw"]))

    assert cache.store == {}, "content-bearing output was written to a cache of unknown persistence"


async def test_content_bearing_node_error_is_redacted_in_the_manifest(monkeypatch: Any) -> None:
    """Regression (independent review, 2026-09-16): a content-bearing node's exception
    message can itself carry raw customer content (a malformed field embedding a secret in
    its own error text) -- and `run()` raises before ever returning a RunResult on
    failure, so the manifest has to be captured independently, the same way the original
    review's regression did (monkeypatching RunManifest.record), to inspect what actually
    got persisted along the failure path."""
    from sixeyes.core.errors import GraphExecutionError
    from sixeyes.obs.trace import RunManifest

    marker = "SYNTHETIC-SECRET-IN-EXCEPTION-MESSAGE"

    @node(output=str, content_bearing=True)
    async def failing_source(ctx: Any) -> str:
        raise ValueError(f"could not parse: {marker}")

    records: list[Any] = []
    original_record = RunManifest.record

    def capture(self: Any, entry: Any) -> None:
        records.append(entry)
        original_record(self, entry)

    monkeypatch.setattr(RunManifest, "record", capture)

    graph = Graph("t")
    graph.add(failing_source("raw"))

    with pytest.raises(GraphExecutionError):
        await Executor(cache=NullCache()).run(graph, targets=["raw"])

    assert records
    assert marker not in repr(records)
    assert all(marker not in (r.error or "") for r in records)
