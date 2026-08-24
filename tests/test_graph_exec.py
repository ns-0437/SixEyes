"""Execution semantics: parallelism, subgraph targeting, failure attribution."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from sixeyes.core.errors import GraphBuildError, GraphExecutionError
from sixeyes.graph import Executor, Graph, MemoryCache, NullCache, node
from tests.conftest import Const, Counter, add, double, make_slow_adder


async def test_diamond_produces_correct_value(diamond: Graph) -> None:
    result = await Executor().run(diamond)
    assert result["join"] == 40  # (10*2) + (10*2)


async def test_independent_nodes_run_concurrently(counter: Counter) -> None:
    """The whole reason for a ready-queue executor.

    Four independent 100ms nodes must finish in roughly 100ms, not 400ms.
    """
    delay = 0.1

    @node(output=int)
    async def slow(ctx: Any, x: int) -> int:
        counter.enter(ctx.current_node)
        try:
            await asyncio.sleep(delay)
            return x
        finally:
            counter.exit()

    graph = Graph("fanout")
    graph.add(Const("src", value=1))
    for i in range(4):
        graph.add(slow(f"slow_{i}"), x="src")
    graph.add(
        add("pair_a"), a="slow_0", b="slow_1"
    )
    graph.add(add("pair_b"), a="slow_2", b="slow_3")
    graph.add(add("join"), a="pair_a", b="pair_b")

    started = time.perf_counter()
    result = await Executor(cache=NullCache(), max_concurrency=8).run(graph)
    elapsed = time.perf_counter() - started

    assert result["join"] == 4
    assert counter.peak == 4, f"expected 4 concurrent nodes, saw {counter.peak}"
    assert elapsed < delay * 2.5, f"took {elapsed:.3f}s; nodes did not overlap"


async def test_concurrency_limit_is_respected(counter: Counter) -> None:
    @node(output=int)
    async def slow(ctx: Any, x: int) -> int:
        counter.enter(ctx.current_node)
        try:
            await asyncio.sleep(0.02)
            return x
        finally:
            counter.exit()

    graph = Graph("bounded")
    graph.add(Const("src", value=1))
    for i in range(8):
        graph.add(slow(f"slow_{i}"), x="src")

    await Executor(cache=NullCache(), max_concurrency=2).run(
        graph, targets=[f"slow_{i}" for i in range(8)]
    )
    assert counter.peak <= 2


async def test_a_node_starts_before_its_whole_level_finishes(counter: Counter) -> None:
    """Ready-queue, not level-synchronous.

    fast -> after is a 2-hop chain; slow is an unrelated long node. 'after' must complete
    while 'slow' is still running, which a level-synchronous scheduler would not allow.
    """
    order: list[str] = []

    @node(output=int)
    async def slow(ctx: Any, x: int) -> int:
        await asyncio.sleep(0.2)
        order.append("slow")
        return x

    @node(output=int)
    async def fast(ctx: Any, x: int) -> int:
        return x

    @node(output=int)
    async def after(ctx: Any, x: int) -> int:
        order.append("after")
        return x

    graph = Graph("ready-queue")
    graph.add(Const("src", value=1))
    graph.add(slow("slow"), x="src")
    graph.add(fast("fast"), x="src")
    graph.add(after("after"), x="fast")
    graph.add(add("join"), a="slow", b="after")

    await Executor(cache=NullCache()).run(graph)
    assert order == ["after", "slow"]


async def test_targets_execute_only_the_needed_subgraph(counter: Counter) -> None:
    """Asking for one detector must not drag in the other four."""

    @node(output=int)
    async def tracked(ctx: Any, x: int) -> int:
        counter.enter(ctx.current_node)
        counter.exit()
        return x

    graph = Graph("subgraph")
    graph.add(Const("src", value=1))
    graph.add(tracked("wanted"), x="src")
    graph.add(tracked("unwanted_a"), x="src")
    graph.add(tracked("unwanted_b"), x="src")

    await Executor(cache=NullCache()).run(graph, targets=["wanted"])

    assert set(counter.calls) == {"wanted"}


async def test_unknown_target_is_rejected(diamond: Graph) -> None:
    with pytest.raises(GraphBuildError):
        await Executor().run(diamond, targets=["nope"])


async def test_failure_names_the_node_and_preserves_the_cause() -> None:
    @node(output=int)
    async def explode(ctx: Any, x: int) -> int:
        raise ValueError("detector hit malformed span")

    graph = Graph("failing")
    graph.add(Const("src", value=1))
    graph.add(explode("boom"), x="src")

    with pytest.raises(GraphExecutionError) as excinfo:
        await Executor(cache=NullCache()).run(graph)

    assert excinfo.value.node_id == "boom"
    assert isinstance(excinfo.value.cause, ValueError)
    assert "malformed span" in str(excinfo.value.cause)


async def test_failure_is_recorded_in_the_manifest() -> None:
    @node(output=int)
    async def explode(ctx: Any, x: int) -> int:
        raise RuntimeError("nope")

    graph = Graph("failing")
    graph.add(Const("src", value=1))
    graph.add(explode("boom"), x="src")

    executor = Executor(cache=NullCache())
    with pytest.raises(GraphExecutionError):
        await executor.run(graph)


async def test_manifest_records_every_node(diamond: Graph) -> None:
    result = await Executor(cache=NullCache()).run(diamond)
    manifest = result.manifest
    assert manifest is not None
    assert {r.node_id for r in manifest.records} == {"src", "left", "right", "join"}
    assert all(r.outcome == "executed" for r in manifest.records)
    assert manifest.duration_ms > 0
    assert "executed" in manifest.summary()
    assert manifest.to_json().startswith("{")


async def test_nodes_receive_their_own_node_id_in_context() -> None:
    seen: list[str] = []

    @node(output=int)
    async def observe(ctx: Any, x: int) -> int:
        seen.append(ctx.current_node)
        return x

    graph = Graph("ctx")
    graph.add(Const("src", value=1))
    graph.add(observe("observer"), x="src")

    await Executor(cache=NullCache()).run(graph)
    assert seen == ["observer"]


async def test_run_sync_wrapper(diamond: Graph) -> None:
    result = await asyncio.to_thread(Executor().run_sync, diamond)
    assert result["join"] == 40


async def test_empty_target_set_is_an_error() -> None:
    graph = Graph("empty")
    with pytest.raises(GraphExecutionError):
        await Executor().run(graph)
