"""Domain model.

Phase 1 defines the vocabulary that findings are expressed in. The trace/request model
arrives in Phase 2; keeping it out of here now avoids inventing a schema before we have
looked at real provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sixeyes.core.ids import Digest, content_hash
from sixeyes.core.units import Money


class Severity(str, Enum):
    """Ordered by how much of a customer's bill the class of defect typically moves."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Confidence(str, Enum):
    """How the recoverable-dollars figure was arrived at.

    Rule 6 of CLAUDE.md: only MEASURED may be presented to a customer as a claim.
    Everything else renders with the word "estimated" attached, in the report, in the
    dashboard, and in the sales deck.
    """

    MEASURED = "measured"      # before/after across a real deploy — a verification row exists
    DERIVED = "derived"        # arithmetic on observed tokens and published prices
    ESTIMATED = "estimated"    # modelled from a sample or a heuristic
    SPECULATIVE = "speculative"  # advisory only; never aggregated into totals


@dataclass(frozen=True, slots=True)
class Evidence:
    """Machine-checkable proof that a defect exists.

    ``locator`` points at a position in a fingerprint (never at content), e.g. a segment
    index and token offset. ``observations`` holds counts and digests only — putting raw
    prompt text here violates rule 3 and is caught by the collector audit test.
    """

    kind: str
    locator: str
    observations: dict[str, Any] = field(default_factory=dict)
    sample_size: int = 0

    def content_key(self) -> Any:
        return [self.kind, self.locator, self.observations, self.sample_size]


@dataclass(frozen=True, slots=True)
class Remediation:
    """What the customer should actually change."""

    summary: str
    steps: tuple[str, ...] = ()
    diff_hint: str | None = None
    effort: str = "unknown"          # "one-line" | "small" | "structural"
    behaviour_preserving: bool = True  # False must never ship before eval infra (rule 1)

    def content_key(self) -> Any:
        return [self.summary, list(self.steps), self.diff_hint, self.effort,
                self.behaviour_preserving]


@dataclass(frozen=True, slots=True)
class Provenance:
    """Which nodes produced this result, and whether any of them was stochastic.

    ``tainted`` is set by the executor, not by node authors. A tainted finding cannot be
    certified — see TaintViolationError.
    """

    node_ids: tuple[str, ...] = ()
    tainted: bool = False
    run_id: str | None = None

    def content_key(self) -> Any:
        return [list(self.node_ids), self.tainted, self.run_id]


@dataclass(frozen=True, slots=True)
class Finding:
    """A unit of recoverable waste.

    Rule 5: evidence, remediation, and money are all mandatory. A finding without them
    does not render.
    """

    detector: str
    title: str
    severity: Severity
    confidence: Confidence
    recoverable: Money
    evidence: Evidence
    remediation: Remediation
    workload_id: str
    provenance: Provenance = field(default_factory=Provenance)

    def __post_init__(self) -> None:
        if self.recoverable.micros < 0:
            raise ValueError("recoverable spend cannot be negative")
        if not self.title.strip():
            raise ValueError("finding requires a title")

    @property
    def id(self) -> Digest:
        """Stable across runs, so a finding can be tracked, snoozed, and verified later."""
        return content_hash(self.detector, self.workload_id, self.evidence)

    @property
    def is_certified(self) -> bool:
        """Deterministically proven and safe to present as fact."""
        return not self.provenance.tainted and self.confidence in (
            Confidence.MEASURED,
            Confidence.DERIVED,
        )

    def content_key(self) -> Any:
        return [self.detector, self.title, self.severity.value, self.confidence.value,
                self.recoverable.micros, self.evidence, self.remediation,
                self.workload_id, self.provenance]


@dataclass(frozen=True, slots=True)
class FindingSet:
    """Detector output. A list plus the totals we are willing to stand behind."""

    findings: tuple[Finding, ...] = ()

    @property
    def certified_total(self) -> Money:
        total = 0
        for finding in self.findings:
            if finding.is_certified:
                total += finding.recoverable.micros
        return Money(total)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self) -> Any:
        return iter(self.findings)

    def content_key(self) -> Any:
        return [list(self.findings)]
