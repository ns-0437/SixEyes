"""Taint propagation: the runtime enforcement of CLAUDE.md rule 2.

An LLM must never be able to back a certified finding, even by accident three refactors
from now. These tests attack that guarantee directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from sixeyes.core.errors import TaintViolationError
from sixeyes.core.types import Confidence, Evidence, Finding, Remediation, Severity
from sixeyes.core.units import Money
from sixeyes.graph import Executor, Graph, NullCache, node
from tests.conftest import Const, double, sampled


def test_stochastic_node_is_tainted() -> None:
    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    assert "llm" in graph.tainted_nodes()


def test_taint_propagates_downstream_transitively() -> None:
    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    graph.add(double("after_1"), x="llm")
    graph.add(double("after_2"), x="after_1")

    tainted = graph.tainted_nodes()
    assert {"llm", "after_1", "after_2"} <= tainted


def test_taint_does_not_leak_upstream_or_sideways() -> None:
    """A sibling branch that never touches the stochastic node must stay clean —
    otherwise every deterministic detector becomes uncertifiable the moment ANY LLM
    node exists anywhere in the graph, which would make certification meaningless."""
    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    graph.add(double("clean_sibling"), x="src")

    tainted = graph.tainted_nodes()
    assert "clean_sibling" not in tainted
    assert "src" not in tainted


def _finding(**overrides: Any) -> Finding:
    base = dict(
        detector="test",
        title="synthetic finding",
        severity=Severity.MEDIUM,
        confidence=Confidence.DERIVED,
        recoverable=Money.from_usd(100),
        evidence=Evidence(kind="test", locator="x"),
        remediation=Remediation(summary="do the thing"),
        workload_id="wl_test",
    )
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


async def test_certify_succeeds_on_a_clean_path() -> None:
    captured: dict[str, Finding] = {}

    @node(output=int)
    async def certifying(ctx: Any, x: int) -> int:
        captured["finding"] = ctx.certify(_finding())
        return x

    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(certifying("detector"), x="src")

    await Executor(cache=NullCache()).run(graph)
    assert captured["finding"].is_certified


async def test_certify_raises_on_a_tainted_path() -> None:
    """The load-bearing assertion in this whole file: a node downstream of an LLM
    cannot certify a finding. It must raise, not silently mark it uncertified — a
    silent downgrade is exactly the kind of bug that ships a false claim to a customer."""

    @node(output=int)
    async def sneaky_detector(ctx: Any, x: int) -> int:
        ctx.certify(_finding())  # must raise before returning
        return x

    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    graph.add(sneaky_detector("detector"), x="llm")

    with pytest.raises(Exception) as excinfo:
        await Executor(cache=NullCache()).run(graph)

    from sixeyes.core.errors import GraphExecutionError

    assert isinstance(excinfo.value, GraphExecutionError)
    assert isinstance(excinfo.value.cause, TaintViolationError)


async def test_manifest_records_which_nodes_were_tainted() -> None:
    graph = Graph("t")
    graph.add(Const("src", value=1))
    graph.add(sampled("llm"), x="src")
    graph.add(double("after"), x="llm")

    result = await Executor(cache=NullCache()).run(graph, targets=["after"])
    assert result.manifest is not None
    assert set(result.manifest.tainted_nodes) == {"llm", "after"}


def test_finding_object_itself_rejects_certification_when_tainted() -> None:
    """Belt and braces: even if someone bypasses ctx.certify and hand-builds
    provenance, Finding.is_certified must still reflect the taint."""
    from sixeyes.core.types import Provenance

    tainted_finding = _finding(provenance=Provenance(("llm",), tainted=True))
    assert not tainted_finding.is_certified
