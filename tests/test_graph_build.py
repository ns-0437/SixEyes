"""Build-time validation.

Every failure in this file must happen before any node executes. A graph that only reveals
a wiring bug forty minutes into a customer's trace export is a graph engine that has failed
at its one job.
"""

from __future__ import annotations

from typing import Any

import pytest

from sixeyes.core.errors import (
    CycleError,
    DuplicateNodeError,
    GraphBuildError,
    PortTypeError,
    UnknownPortError,
)
from sixeyes.graph import Graph, NodeKind, node
from tests.conftest import Const, add, double, sampled, stringify


def test_valid_graph_validates_and_is_idempotent(diamond: Graph) -> None:
    assert diamond.validate() is diamond
    assert diamond.validate() is diamond
    assert len(diamond) == 4


def test_sinks_are_the_nodes_nothing_consumes(diamond: Graph) -> None:
    assert diamond.sinks == ("join",)


def test_cycle_is_detected_and_named() -> None:
    graph = Graph("cyclic")
    graph.add(double("a"), x="c")
    graph.add(double("b"), x="a")
    graph.add(double("c"), x="b")

    with pytest.raises(CycleError) as excinfo:
        graph.validate()

    assert set(excinfo.value.cycle) == {"a", "b", "c"}
    assert "->" in str(excinfo.value)


def test_self_loop_is_a_cycle() -> None:
    graph = Graph("self")
    graph.add(double("a"), x="a")
    with pytest.raises(CycleError):
        graph.validate()


def test_type_mismatch_is_rejected_with_both_type_names() -> None:
    graph = Graph("mistyped")
    graph.add(Const("src", value=1))
    graph.add(stringify("s"), x="src")     # int -> str, fine
    graph.add(double("d"), x="s")          # str -> int, not fine

    with pytest.raises(PortTypeError) as excinfo:
        graph.validate()

    message = str(excinfo.value)
    assert "int" in message and "str" in message and "'d'" in message


def test_unknown_port_is_rejected() -> None:
    graph = Graph("bad-port")
    graph.add(Const("src", value=1))
    graph.add(double("d"), wrong_name="src")

    with pytest.raises(UnknownPortError) as excinfo:
        graph.validate()
    assert "wrong_name" in str(excinfo.value)


def test_unwired_port_is_rejected() -> None:
    graph = Graph("unwired")
    graph.add(Const("src", value=1))
    graph.add(add("a"), a="src")  # 'b' never wired

    with pytest.raises(GraphBuildError) as excinfo:
        graph.validate()
    assert "'b'" in str(excinfo.value)


def test_reference_to_unknown_node_is_rejected() -> None:
    graph = Graph("dangling")
    graph.add(double("d"), x="nope")
    with pytest.raises(GraphBuildError) as excinfo:
        graph.validate()
    assert "nope" in str(excinfo.value)


def test_duplicate_node_id_is_rejected() -> None:
    graph = Graph("dupes")
    graph.add(Const("src", value=1))
    with pytest.raises(DuplicateNodeError):
        graph.add(Const("src", value=2))


def test_invalid_node_id_is_rejected() -> None:
    with pytest.raises(GraphBuildError):
        Const("has spaces", value=1)
    with pytest.raises(GraphBuildError):
        Const("", value=1)


def test_node_decorator_requires_async_function() -> None:
    with pytest.raises(GraphBuildError):

        @node(output=int)
        def not_async(ctx: Any, x: int) -> int:  # type: ignore[misc]
            return x


def test_node_decorator_requires_ctx_first() -> None:
    with pytest.raises(GraphBuildError):

        @node(output=int)
        async def no_ctx(x: int) -> int:
            return x


def test_node_decorator_requires_annotations() -> None:
    with pytest.raises(GraphBuildError):

        @node(output=int)
        async def unannotated(ctx: Any, x) -> int:  # type: ignore[no-untyped-def]
            return x


def test_levels_group_by_dependency_depth(diamond: Graph) -> None:
    assert diamond.levels() == (("src",), ("left", "right"), ("join",))


def test_execution_order_is_deterministic(diamond: Graph) -> None:
    orders = {diamond.execution_order() for _ in range(20)}
    assert len(orders) == 1
    order = orders.pop()
    assert order.index("src") < order.index("left") < order.index("join")


def test_describe_renders_plan_with_taint_marks() -> None:
    graph = Graph("described")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    graph.add(double("after"), x="llm")

    text = graph.describe()
    assert "src" in text and "stochastic" in text
    assert text.count("[tainted]") == 2  # llm and everything downstream
