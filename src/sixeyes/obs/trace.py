"""Run manifest.

Every run records what executed, at what version, under what cache key, and whether it was
tainted. This exists for one commercial reason: when a customer disputes a dollar figure,
we can reproduce exactly how it was derived. "Trust our dashboard" does not survive a
technical review.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sixeyes.core.ids import Digest

NodeOutcome = Literal["executed", "cache_hit", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class NodeRecord:
    node_id: str
    kind: str
    version: str
    cache_key: Digest
    outcome: NodeOutcome
    duration_ms: float
    tainted: bool
    content_bearing: bool = False
    error: str | None = None


@dataclass(slots=True)
class RunManifest:
    run_id: str
    graph_name: str
    started_at: float
    finished_at: float | None = None
    records: list[NodeRecord] = field(default_factory=list)

    def record(self, entry: NodeRecord) -> None:
        self.records.append(entry)

    @property
    def duration_ms(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at) * 1000

    @property
    def cache_hit_rate(self) -> float:
        considered = [r for r in self.records if r.outcome in ("executed", "cache_hit")]
        if not considered:
            return 0.0
        hits = sum(1 for r in considered if r.outcome == "cache_hit")
        return hits / len(considered)

    @property
    def failed(self) -> tuple[NodeRecord, ...]:
        return tuple(r for r in self.records if r.outcome == "failed")

    @property
    def tainted_nodes(self) -> tuple[str, ...]:
        return tuple(r.node_id for r in self.records if r.tainted)

    @property
    def content_bearing_nodes(self) -> tuple[str, ...]:
        """Nodes whose output may hold raw customer content. Cross-reference against
        `cache_key` outcomes to audit that none of these ever landed in a persistent
        cache -- exactly the claim CLAUDE.md rule 3 makes."""
        return tuple(r.node_id for r in self.records if r.content_bearing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph": self.graph_name,
            "duration_ms": round(self.duration_ms, 2),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "tainted_nodes": list(self.tainted_nodes),
            "nodes": [asdict(r) for r in self.records],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def summary(self) -> str:
        executed = sum(1 for r in self.records if r.outcome == "executed")
        hits = sum(1 for r in self.records if r.outcome == "cache_hit")
        failed = len(self.failed)
        parts = [
            f"run {self.run_id}",
            f"{self.duration_ms:.0f}ms",
            f"{executed} executed",
            f"{hits} cached",
        ]
        if failed:
            parts.append(f"{failed} FAILED")
        if self.tainted_nodes:
            parts.append(f"{len(self.tainted_nodes)} tainted")
        return "  ".join(parts)
