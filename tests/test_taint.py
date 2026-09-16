"""Taint propagation: the runtime enforcement of CLAUDE.md rule 2.

An LLM must never be able to back a certified finding, even by accident three refactors
from now. These tests attack that guarantee directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from sixeyes.core.errors import TaintViolationError
from sixeyes.core.types import Confidence, Evidence, Finding, FindingSet, Remediation, Severity
from sixeyes.core.units import Money
from sixeyes.graph import Executor, Graph, NodeKind, NullCache, node
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


async def test_executor_detaints_a_finding_that_skipped_ctx_certify() -> None:
    """Regression (independent review, 2026-09-16): `ctx.certify` was opt-in, not
    enforced. A STOCHASTIC node that hand-built and returned a Finding directly --
    skipping ctx.certify entirely -- kept its default Provenance(tainted=False), so
    `finding.is_certified` was True even though an LLM produced it. The executor now
    forces `tainted=True` on every Finding reachable in a tainted node's output
    unconditionally, regardless of whether the node called ctx.certify at all -- this is
    the actual enforcement of rule 2; ctx.certify is a convenience for the clean path,
    not the safety mechanism."""

    @node(output=Finding, kind=NodeKind.STOCHASTIC)
    async def sneaky_stochastic(ctx: Any) -> Finding:
        return _finding()  # deliberately bypasses ctx.certify

    graph = Graph("t")
    graph.add(sneaky_stochastic("llm"))

    result = await Executor(cache=NullCache()).run(graph)

    assert result["llm"].provenance.tainted is True
    assert not result["llm"].is_certified


async def test_executor_detaints_findings_nested_in_a_tuple_or_findingset() -> None:
    """The same bypass, but the tainted node returns a collection of findings rather than
    a single one -- the detaint walk must recurse into tuples and FindingSet, not just
    handle the single-Finding case."""

    @node(output=tuple, kind=NodeKind.STOCHASTIC)
    async def sneaky_stochastic_batch(ctx: Any) -> tuple[Finding, ...]:
        return (_finding(), _finding())

    @node(output=FindingSet, kind=NodeKind.STOCHASTIC)
    async def sneaky_stochastic_set(ctx: Any) -> FindingSet:
        return FindingSet(findings=(_finding(),))

    graph = Graph("t")
    graph.add(sneaky_stochastic_batch("batch"))
    graph.add(sneaky_stochastic_set("set"))

    result = await Executor(cache=NullCache()).run(graph, targets=["batch", "set"])

    assert all(f.provenance.tainted and not f.is_certified for f in result["batch"])
    assert all(f.provenance.tainted and not f.is_certified for f in result["set"])


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
