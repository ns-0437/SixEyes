from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sixeyes.graph import Graph, Node, NodeKind, node


class Const(Node):
    """A SOURCE that yields a fixed value. Config participates in the cache key, so two
    Consts with different values get different keys."""

    kind = NodeKind.SOURCE
    version = "1"
    inputs: dict[str, type] = {}
    output = int

    async def execute(self, ctx: Any, **_: Any) -> int:
        return int(self.config["value"])


class Counter:
    """Records how many times each node body actually ran, and peak concurrency."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.active = 0
        self.peak = 0

    def enter(self, node_id: str) -> None:
        self.calls[node_id] = self.calls.get(node_id, 0) + 1
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self) -> None:
        self.active -= 1

    def total(self) -> int:
        return sum(self.calls.values())


@pytest.fixture
def counter() -> Counter:
    return Counter()


def make_slow_adder(counter: Counter, delay: float = 0.05):
    """Adds its inputs after a delay, so tests can observe real concurrency."""

    @node(output=int)
    async def slow_add(ctx: Any, a: int, b: int) -> int:
        counter.enter(ctx.current_node)
        try:
            await asyncio.sleep(delay)
            return a + b
        finally:
            counter.exit()

    return slow_add


@node(output=int)
async def add(ctx: Any, a: int, b: int) -> int:
    return a + b


@node(output=int)
async def double(ctx: Any, x: int) -> int:
    return x * 2


@node(output=str)
async def stringify(ctx: Any, x: int) -> str:
    return str(x)


@node(output=int, kind=NodeKind.STOCHASTIC)
async def sampled(ctx: Any, x: int) -> int:
    """Stands in for an LLM call: same inputs, different answer."""
    return x + 1


@pytest.fixture
def diamond() -> Graph:
    """left and right both depend on src, and join depends on both.

        src -> left  -\\
            -> right ---> join
    """
    graph = Graph("diamond")
    graph.add(Const("src", value=10))
    graph.add(double("left"), x="src")
    graph.add(double("right"), x="src")
    graph.add(add("join"), a="left", b="right")
    return graph
