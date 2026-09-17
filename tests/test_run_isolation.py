"""Run isolation: rejecting overlapping runs that share stateful node instances.

Regression (independent fourth-round review, 2026-09-17): RunScoped (graph/run_scoped.py)
makes a node's cache-key computation and its execution agree on one value *within* one
run, but says nothing about two *overlapping* runs sharing the same node instances. A run
A that plans from file content "alpha" and pauses mid-execution could have run B --
sharing the same node instances -- refresh that node's RunScoped state to "bravo" before A
resumed; A then silently consumed "bravo" under its own "alpha" cache identity, and a
later run got that wrong result served back from cache.

Real per-run isolation (each run holding its own copy of prepared state) is out of scope
for the first version. The fix is the smallest correct one, per the review's own
recommendation: refuse the overlap outright.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from sixeyes.fingerprint.fingerprint import Fingerprint, fingerprint_request
from sixeyes.graph import DiskCache, Executor, Graph, MemoryCache, NodeKind, OverlappingRunError, node
from sixeyes.graph.run_isolation import _claimed
from sixeyes.ingest.jsonl import JsonlSource


def _write(path: Path, system: str) -> None:
    path.write_text(
        json.dumps({"request_id": "r", "timestamp": 1.0, "model": "m",
                    "system": system, "messages": []}),
        encoding="utf-8",
    )


async def test_overlapping_run_is_rejected_and_the_first_run_stays_correct(tmp_path: Path) -> None:
    """The core acceptance criterion: either both runs retain their own prepared values,
    or the overlapping call receives an explicit rejection and the first run remains
    correct. This chooses rejection (per the review's own recommended MVP fix) and checks
    both halves: run A is never corrupted, and a subsequent run is not poisoned either."""
    path = tmp_path / "trace.jsonl"
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class GatedSource(JsonlSource):
        async def execute(self, ctx: Any, **kwargs: Any) -> Any:
            if not first_started.is_set():
                first_started.set()
                await release_first.wait()
            return await super().execute(ctx, **kwargs)

    graph = Graph("overlap")
    graph.add(GatedSource("source", path=str(path)))
    graph.add(Fingerprint("fingerprint", key=b"\x01" * 32), trace="source")
    executor = Executor(cache=DiskCache(tmp_path / "cache"))

    _write(path, "alpha")
    first_task = asyncio.create_task(executor.run(graph))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=5)
        # Run A has already planned from "alpha" and is paused inside execute(). Run B,
        # sharing the same node instances, must be rejected before it can touch anything.
        _write(path, "bravo")
        with pytest.raises(OverlappingRunError) as excinfo:
            await executor.run(graph)
        assert "source" in str(excinfo.value)
    finally:
        release_first.set()

    first = await first_task
    # Run A must have consumed its OWN "alpha" snapshot, never touched by the rejected B.
    assert first["source"].requests[0].system == "alpha"

    # A subsequent run must not be poisoned by anything from the rejected overlap.
    _write(path, "alpha")
    third = await executor.run(graph)
    expected = fingerprint_request(third["source"].requests[0], b"\x01" * 32)
    assert third["fingerprint"][0].segments == expected.segments


async def test_rejected_run_claims_nothing(tmp_path: Path) -> None:
    """A run that gets rejected must not leave a partial claim behind -- claim() is
    all-or-nothing, checked before anything is added to the registry."""
    started = asyncio.Event()

    @node(output=int)
    async def slow(ctx: Any) -> int:
        started.set()
        await asyncio.sleep(0.2)
        return 1

    graph = Graph("reject")
    graph.add(slow("s"))
    executor = Executor(cache=MemoryCache())

    task = asyncio.create_task(executor.run(graph))
    await started.wait()

    with pytest.raises(OverlappingRunError):
        await executor.run(graph)

    await task  # first run still completes normally
    await asyncio.sleep(0)
    assert not any(n.node_id == "s" for n in _claimed)


async def test_claim_is_released_after_cancellation(tmp_path: Path) -> None:
    """Acceptance criterion: cleanup after cancellation, not just after a clean finish or
    a raised exception."""
    started = asyncio.Event()

    @node(output=int)
    async def hangs(ctx: Any) -> int:
        started.set()
        await asyncio.sleep(100)
        return 1

    graph = Graph("cancel")
    graph.add(hangs("n"))
    executor = Executor(cache=MemoryCache())

    task = asyncio.create_task(executor.run(graph))
    await started.wait()
    assert any(n.node_id == "n" for n in _claimed)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert not any(n.node_id == "n" for n in _claimed)


async def test_claim_is_released_after_a_node_raises(tmp_path: Path) -> None:
    """Acceptance criterion: cleanup after a genuine node failure, not just after a
    clean finish."""

    @node(output=int)
    async def fails(ctx: Any) -> int:
        raise RuntimeError("boom")

    graph = Graph("fail")
    graph.add(fails("m"))
    executor = Executor(cache=MemoryCache())

    with pytest.raises(Exception):
        await executor.run(graph)

    assert not any(n.node_id == "m" for n in _claimed)


async def test_independent_graphs_with_independent_node_instances_never_block_each_other() -> None:
    """The claim registry is per node *instance*, not per Graph or Executor -- two
    completely separate graphs (even with identically-named nodes) must run concurrently
    without tripping the overlap guard at all."""

    @node(output=int)
    async def slow(ctx: Any) -> int:
        await asyncio.sleep(0.05)
        return 1

    graph_a = Graph("a")
    graph_a.add(slow("shared_name"))
    graph_b = Graph("b")
    graph_b.add(slow("shared_name"))

    results = await asyncio.gather(
        Executor(cache=MemoryCache()).run(graph_a),
        Executor(cache=MemoryCache()).run(graph_b),
    )
    assert results[0]["shared_name"] == 1
    assert results[1]["shared_name"] == 1


async def test_sequential_runs_on_the_same_graph_are_never_rejected() -> None:
    """The common case: one run completes fully before the next starts. This must never
    be mistaken for an overlap."""

    @node(output=int)
    async def quick(ctx: Any) -> int:
        return 1

    graph = Graph("sequential")
    graph.add(quick("q"))
    executor = Executor(cache=MemoryCache())

    for _ in range(5):
        result = await executor.run(graph)
        assert result["q"] == 1
