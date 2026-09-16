"""Incremental caching.

This is the commercial justification for owning a graph engine at all: adding a sixth
detector must re-run one node, not re-fingerprint the whole trace. These tests assert that
property directly by counting real executions, not by reasoning about it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from sixeyes.graph import DiskCache, Executor, Graph, MemoryCache, node
from tests.conftest import Const, Counter, add, double


def _tracked_double(counter: Counter, version: str = "1"):
    @node(output=int, version=version)
    async def tracked_double(ctx: Any, x: int) -> int:
        counter.enter(ctx.current_node)
        counter.exit()
        return x * 2

    return tracked_double


async def test_second_run_is_fully_cached(counter: Counter) -> None:
    graph = Graph("cached")
    graph.add(Const("src", value=5))
    graph.add(_tracked_double(counter)("d"), x="src")
    graph.add(add("j"), a="d", b="src")

    cache = MemoryCache()
    r1 = await Executor(cache=cache).run(graph)
    r2 = await Executor(cache=cache).run(graph)

    assert r1["j"] == r2["j"] == 15
    assert counter.calls == {"d": 1}  # only the first run actually executed it
    assert r2.manifest is not None
    assert all(rec.outcome == "cache_hit" for rec in r2.manifest.records)


async def test_changing_upstream_config_invalidates_downstream(counter: Counter) -> None:
    cache = MemoryCache()

    graph_a = Graph("g")
    graph_a.add(Const("src", value=5))
    graph_a.add(_tracked_double(counter)("d"), x="src")
    await Executor(cache=cache).run(graph_a)

    graph_b = Graph("g")
    graph_b.add(Const("src", value=9))  # different config -> different content hash
    graph_b.add(_tracked_double(counter)("d"), x="src")
    result_b = await Executor(cache=cache).run(graph_b)

    assert result_b["d"] == 18
    assert counter.calls == {"d": 2}, "changed upstream must invalidate its dependent"


async def test_bumping_node_version_invalidates_only_that_lineage(counter: Counter) -> None:
    """The whole incremental-analysis story in one test.

    left and right both hang off src. Bumping left's version must re-run left (and join,
    which depends on it) but must NOT re-run right or re-fetch src's cached artifact.
    """
    cache = MemoryCache()

    def build(left_version: str) -> Graph:
        g = Graph("v")
        g.add(Const("src", value=10))
        g.add(_tracked_double(counter, version=left_version)("left"), x="src")
        g.add(_tracked_double(counter)("right"), x="src")
        g.add(add("join"), a="left", b="right")
        return g

    await Executor(cache=cache).run(build("1"))
    counter.calls.clear()

    result = await Executor(cache=cache).run(build("2"))  # only left's version changes

    assert result["join"] == 40
    assert counter.calls == {"left": 1}, (
        f"expected only 'left' to re-execute, got {counter.calls}"
    )


async def test_disk_cache_survives_process_restart(tmp_path: Path, counter: Counter) -> None:
    cache_dir = tmp_path / "cache"

    graph = Graph("disk")
    graph.add(Const("src", value=7))
    graph.add(_tracked_double(counter)("d"), x="src")

    cache1 = DiskCache(cache_dir)
    r1 = await Executor(cache=cache1).run(graph)
    assert r1["d"] == 14
    assert counter.calls == {"d": 1}

    # Simulate a fresh process: new DiskCache instance, same directory, no memory carried
    # over from cache1.
    cache2 = DiskCache(cache_dir)
    r2 = await Executor(cache=cache2).run(graph)
    assert r2["d"] == 14
    assert counter.calls == {"d": 1}, "disk cache should have served this without re-executing"


async def test_disk_cache_survives_a_corrupt_entry(tmp_path: Path, counter: Counter) -> None:
    cache_dir = tmp_path / "cache"
    graph = Graph("disk-corrupt")
    graph.add(Const("src", value=3))
    graph.add(_tracked_double(counter)("d"), x="src")

    cache = DiskCache(cache_dir)
    keys = graph.cache_keys()
    await Executor(cache=cache).run(graph)

    # Corrupt the on-disk entry directly, bypassing the memo layer.
    path = cache._path(keys["d"])
    path.write_bytes(b"not a pickle")
    cache._memo.clear()

    result = await Executor(cache=DiskCache(cache_dir)).run(graph)
    assert result["d"] == 6  # recomputed, did not crash


async def test_resident_kind_is_never_cached() -> None:
    """RESIDENT nodes own their own state; the graph cache must not intercept them."""
    from sixeyes.graph import NodeKind

    calls = {"n": 0}

    @node(output=int, kind=NodeKind.RESIDENT)
    async def counting(ctx: Any, x: int) -> int:
        calls["n"] += 1
        return calls["n"]

    graph = Graph("resident")
    graph.add(Const("src", value=1))
    graph.add(counting("r"), x="src")

    cache = MemoryCache()
    r1 = await Executor(cache=cache).run(graph)
    r2 = await Executor(cache=cache).run(graph)
    assert (r1["r"], r2["r"]) == (1, 2), "RESIDENT output must not be cached"


async def test_pure_descendant_of_resident_is_not_stale() -> None:
    """Regression (independent review, 2026-09-16): a PURE node's cache key is derived
    from node *identity*, computed once before any node executes -- it has no way to see
    that a RESIDENT upstream's actual output changed between two separate `Executor.run()`
    calls sharing one cache. A PURE descendant's key was therefore identical across runs
    even though its real input differed, and it served a stale cached result. Fixed by
    excluding RESIDENT nodes and everything downstream of them from caching entirely
    (Graph.cache_unsafe_nodes) -- the same fail-closed default `content_bearing` gets."""
    from sixeyes.graph import NodeKind

    calls = {"n": 0}

    @node(output=int, kind=NodeKind.RESIDENT)
    async def resident(ctx: Any) -> int:
        calls["n"] += 1
        return calls["n"]

    @node(output=int)
    async def doubled(ctx: Any, x: int) -> int:
        return 2 * x

    graph = Graph("resident_descendant")
    graph.add(resident("source"))
    graph.add(doubled("double"), x="source")

    cache = MemoryCache()
    executor = Executor(cache=cache)
    r1 = await executor.run(graph)
    r2 = await executor.run(graph)

    assert (r1["source"], r1["double"]) == (1, 2)
    assert (r2["source"], r2["double"]) == (2, 4), "double() served a stale result"


async def test_pure_descendant_of_stochastic_is_not_stale() -> None:
    """Regression (independent follow-up review, 2026-09-16): the RESIDENT fix above
    didn't extend to STOCHASTIC, which has the identical structural problem -- a PURE
    descendant's cache key can't see that a STOCHASTIC upstream's sample changed between
    two runs. Graph.cache_unsafe_nodes now covers both kinds."""
    from sixeyes.graph import NodeKind

    calls = {"n": 0}

    @node(output=int, kind=NodeKind.STOCHASTIC)
    async def sample(ctx: Any) -> int:
        calls["n"] += 1
        return calls["n"]

    @node(output=int)
    async def doubled(ctx: Any, x: int) -> int:
        return 2 * x

    graph = Graph("stochastic_descendant")
    graph.add(sample("source"))
    graph.add(doubled("double"), x="source")

    cache = MemoryCache()
    executor = Executor(cache=cache)
    r1 = await executor.run(graph)
    r2 = await executor.run(graph)

    assert (r1["source"], r1["double"]) == (1, 2)
    assert (r2["source"], r2["double"]) == (2, 4), "double() served a stale result"
