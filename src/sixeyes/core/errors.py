"""Exception hierarchy. Build-time errors are distinct from run-time errors on purpose:
graph construction must fail loudly before any I/O happens."""

from __future__ import annotations


class SextantError(Exception):
    """Base for every error this package raises."""


class GraphBuildError(SextantError):
    """Raised while assembling or validating a graph. Never raised during execution."""


class CycleError(GraphBuildError):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__("cycle detected: " + " -> ".join([*cycle, cycle[0]]))


class PortTypeError(GraphBuildError):
    """A wired edge has incompatible producer/consumer types."""


class UnknownPortError(GraphBuildError):
    """An edge references a port the node does not declare."""


class DuplicateNodeError(GraphBuildError):
    pass


class GraphExecutionError(SextantError):
    """A node raised. Carries the node id so failures are attributable."""

    def __init__(self, node_id: str, cause: BaseException) -> None:
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"node {node_id!r} failed: {cause!r}")


class TaintViolationError(SextantError):
    """Attempted to certify a result whose provenance includes a STOCHASTIC node.

    This is rule 2 of CLAUDE.md enforced at runtime: an LLM may never sit in the
    detection path, and the engine refuses to pretend otherwise.
    """
