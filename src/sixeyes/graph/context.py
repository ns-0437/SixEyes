"""Per-run execution context.

Handed to every node's ``execute``. Deliberately small: nodes get logging, taint queries,
and scratch space, and nothing that would let them reach around the graph to fetch another
node's output. Dependencies flow through declared ports or not at all — that constraint is
what keeps the cache keys honest.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sixeyes.core.errors import TaintViolationError
from sixeyes.core.types import Finding, Provenance

logger = logging.getLogger("sixeyes")


@dataclass(slots=True)
class RunContext:
    """State shared across one graph execution."""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    graph_name: str = "analysis"
    tainted_nodes: frozenset[str] = frozenset()
    current_node: str = ""
    scratch: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tainted(self) -> bool:
        """Whether the node currently executing sits downstream of any LLM."""
        return self.current_node in self.tainted_nodes

    def provenance(self, *upstream_node_ids: str) -> Provenance:
        """Build provenance for a result produced by the current node."""
        chain = (*upstream_node_ids, self.current_node) if self.current_node else upstream_node_ids
        tainted = any(n in self.tainted_nodes for n in chain)
        return Provenance(node_ids=chain, tainted=tainted, run_id=self.run_id)

    def certify(self, finding: Finding) -> Finding:
        """Stamp provenance onto a finding, refusing if the path is tainted.

        This is the runtime half of rule 2 in CLAUDE.md. A detector that has quietly grown
        an LLM dependency cannot publish a certified finding — it raises here instead,
        loudly, in tests, long before it reaches a customer report.
        """
        if self.is_tainted:
            raise TaintViolationError(
                f"node {self.current_node!r} is downstream of a stochastic node and "
                f"cannot certify finding {finding.title!r}. Detection must be "
                f"deterministic; move this to the remediation stage."
            )
        from dataclasses import replace

        return replace(finding, provenance=self.provenance())

    def log(self, message: str, *args: Any) -> None:
        logger.info("[%s/%s] " + message, self.run_id, self.current_node or "-", *args)

    def warn(self, message: str, *args: Any) -> None:
        logger.warning("[%s/%s] " + message, self.run_id, self.current_node or "-", *args)

    def for_node(self, node_id: str) -> RunContext:
        """A view of this context bound to one node. Scratch is shared by reference so
        RESIDENT nodes can stash state; everything else is per-node."""
        return RunContext(
            run_id=self.run_id,
            graph_name=self.graph_name,
            tainted_nodes=self.tainted_nodes,
            current_node=node_id,
            scratch=self.scratch,
        )
