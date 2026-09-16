"""Graph construction and static validation.

Everything expensive in this file happens before any I/O. By the time the executor starts,
we already know the graph is acyclic, fully wired, type-consistent, which nodes are
tainted, and what every node's cache key is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from sixeyes.core.errors import (
    CycleError,
    DuplicateNodeError,
    GraphBuildError,
    PortTypeError,
    UnknownPortError,
)
from sixeyes.core.ids import Digest, content_hash
from sixeyes.graph.node import Node, NodeKind


def _types_compatible(produced: Any, expected: Any) -> bool:
    """Conservative structural check.

    Concrete classes are checked with issubclass. Anything typing-generic (list[int],
    Protocol, unions) is accepted: a partial check that catches the common wiring mistake
    beats a strict one that forces node authors to fight the annotation system.
    """
    if expected is Any or expected is object:
        return True
    if not isinstance(produced, type) or not isinstance(expected, type):
        return True
    return issubclass(produced, expected)


@dataclass(frozen=True, slots=True)
class Edge:
    upstream: str
    downstream: str
    port: str


class Graph:
    """A validated DAG of nodes.

    Build with ``add``, then call ``validate``. Validation is idempotent and is invoked
    automatically by the executor, so forgetting it cannot produce a partially-checked run.
    """

    def __init__(self, name: str = "analysis") -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._wiring: dict[str, dict[str, str]] = {}
        self._validated = False

    # ------------------------------------------------------------------ construction

    def add(self, node: Node, **wiring: str) -> Node:
        """Register a node and wire its input ports to upstream node ids.

        graph.add(Fingerprint("fingerprint"), requests="normalise")
        """
        if node.node_id in self._nodes:
            raise DuplicateNodeError(f"node id {node.node_id!r} already registered")
        self._nodes[node.node_id] = node
        self._wiring[node.node_id] = dict(wiring)
        self._validated = False
        return node

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[Node]:
        return iter(self._nodes.values())

    def node(self, node_id: str) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise GraphBuildError(f"unknown node {node_id!r}") from None

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def upstreams(self, node_id: str) -> Mapping[str, str]:
        return self._wiring[node_id]

    def dependents(self, node_id: str) -> tuple[str, ...]:
        return tuple(
            downstream
            for downstream, ports in self._wiring.items()
            if node_id in ports.values()
        )

    @property
    def sinks(self) -> tuple[str, ...]:
        """Nodes nothing depends on. These are what the executor is asked to produce."""
        consumed = {up for ports in self._wiring.values() for up in ports.values()}
        return tuple(n for n in self._nodes if n not in consumed)

    # ------------------------------------------------------------------ validation

    def validate(self) -> Graph:
        if self._validated:
            return self
        self._check_ports()
        self._topological_order()  # raises CycleError
        self._validated = True
        return self

    def _check_ports(self) -> None:
        for node_id, node in self._nodes.items():
            wiring = self._wiring[node_id]

            for port in wiring:
                if port not in node.inputs:
                    declared = sorted(node.inputs) or ["(none)"]
                    raise UnknownPortError(
                        f"{node_id!r} has no input port {port!r}; "
                        f"declared ports: {declared}"
                    )

            for port, expected in node.inputs.items():
                if port not in wiring:
                    raise GraphBuildError(f"{node_id!r} input port {port!r} is not wired")
                upstream_id = wiring[port]
                if upstream_id not in self._nodes:
                    raise GraphBuildError(
                        f"{node_id!r}.{port} references unknown node {upstream_id!r}"
                    )
                produced = self._nodes[upstream_id].output
                if not _types_compatible(produced, expected):
                    want = getattr(expected, "__name__", expected)
                    got = getattr(produced, "__name__", produced)
                    raise PortTypeError(
                        f"{node_id!r}.{port} expects {want} "
                        f"but {upstream_id!r} produces {got}"
                    )

    def _topological_order(self) -> tuple[str, ...]:
        """Kahn's algorithm. Ties break by node id so ordering is deterministic, which
        keeps run manifests diffable across runs."""
        indegree = {n: len(set(self._wiring[n].values())) for n in self._nodes}
        ready = sorted(n for n, d in indegree.items() if d == 0)
        order: list[str] = []

        while ready:
            current = ready.pop(0)
            order.append(current)
            for downstream in sorted(self.dependents(current)):
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
            ready.sort()

        if len(order) != len(self._nodes):
            raise CycleError(self._find_cycle())
        return tuple(order)

    def _find_cycle(self) -> list[str]:
        """Depth-first walk to name the actual cycle. An error that says 'cycle detected'
        without naming the nodes is a bad error."""
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            state[node_id] = 1
            stack.append(node_id)
            for upstream in sorted(set(self._wiring[node_id].values())):
                if state.get(upstream, 0) == 1:
                    return stack[stack.index(upstream) :]
                if state.get(upstream, 0) == 0:
                    found = visit(upstream)
                    if found:
                        return found
            stack.pop()
            state[node_id] = 2
            return None

        for node_id in sorted(self._nodes):
            if state.get(node_id, 0) == 0:
                cycle = visit(node_id)
                if cycle:
                    return cycle
        return sorted(self._nodes)

    # ------------------------------------------------------------------ derived plans

    def execution_order(self) -> tuple[str, ...]:
        self.validate()
        return self._topological_order()

    def levels(self) -> tuple[tuple[str, ...], ...]:
        """Nodes grouped by dependency depth. Purely diagnostic — the executor uses a
        ready-queue so a node starts the instant its own dependencies resolve, rather
        than waiting for its whole level."""
        self.validate()
        depth: dict[str, int] = {}
        for node_id in self._topological_order():
            ups = set(self._wiring[node_id].values())
            depth[node_id] = 0 if not ups else 1 + max(depth[u] for u in ups)
        grouped: dict[int, list[str]] = {}
        for node_id, d in depth.items():
            grouped.setdefault(d, []).append(node_id)
        return tuple(tuple(sorted(grouped[d])) for d in sorted(grouped))

    def cache_keys(self) -> dict[str, Digest]:
        """Content-addressed key per node.

        key(n) = H(identity(n), [(port, key(upstream)) ...])

        A node's key changes only when the node itself changes or something it actually
        depends on changes. That is what makes adding a sixth detector re-run one node
        instead of re-fingerprinting two million spans.
        """
        self.validate()
        keys: dict[str, Digest] = {}
        for node_id in self._topological_order():
            upstream_keys = [
                [port, keys[upstream]]
                for port, upstream in sorted(self._wiring[node_id].items())
            ]
            keys[node_id] = content_hash(self._nodes[node_id].identity(), upstream_keys)
        return keys

    def tainted_nodes(self) -> frozenset[str]:
        """Transitive closure of stochastic influence.

        Anything downstream of an LLM is tainted forever. The executor uses this to refuse
        certification, so rule 2 of CLAUDE.md cannot be violated by accident.
        """
        self.validate()
        tainted: set[str] = set()
        for node_id in self._topological_order():
            node = self._nodes[node_id]
            if node.kind.taints or any(
                up in tainted for up in self._wiring[node_id].values()
            ):
                tainted.add(node_id)
        return frozenset(tainted)

    def content_bearing_nodes(self) -> frozenset[str]:
        """Transitive closure of content-bearing influence, honouring declassification.

        A node's *effective* content-bearing status is its own declared status, OR any
        upstream's effective status -- UNLESS this node explicitly declares
        `declassifies = True`, which resets the closure at that point. This is the fix for
        an ordinary passthrough node silently persisting raw content to disk simply
        because it never set `content_bearing = True` itself: by default, sensitivity
        propagates; only a reviewed, explicit declassification stops it.
        """
        self.validate()
        content_bearing: set[str] = set()
        for node_id in self._topological_order():
            node = self._nodes[node_id]
            upstream_bearing = any(
                up in content_bearing for up in self._wiring[node_id].values()
            )
            if node.declassifies:
                effective = node.content_bearing
            else:
                effective = node.content_bearing or upstream_bearing
            if effective:
                content_bearing.add(node_id)
        return frozenset(content_bearing)

    def cache_unsafe_nodes(self) -> frozenset[str]:
        """Nodes whose structural (identity-based) cache key cannot be trusted to reflect
        their actual runtime output: RESIDENT nodes themselves, and everything downstream
        of one.

        RESIDENT nodes may return a different value on every invocation by design (an
        index, a baseline) while `Graph.cache_keys()` is computed once from node identity,
        before any node has executed -- it has no way to see that a RESIDENT node's output
        actually changed between two separate `Executor.run()` calls sharing one cache. A
        PURE descendant's key is therefore stable across runs even when its real input
        changed, which serves a stale result. Until cache keys can incorporate a RESIDENT
        node's actual output (a real fix, not yet built), the safe interim behaviour is to
        never persist-or-reuse a cached result for anything in this closure -- always
        recompute, the same conservative default `content_bearing` gets under a
        persistent cache.
        """
        self.validate()
        unsafe: set[str] = set()
        for node_id in self._topological_order():
            node = self._nodes[node_id]
            if node.kind is NodeKind.RESIDENT or any(
                up in unsafe for up in self._wiring[node_id].values()
            ):
                unsafe.add(node_id)
        return frozenset(unsafe)

    def describe(self) -> str:
        """Human-readable plan. Printed by the CLI before a run so a customer's engineer
        can see exactly what is about to execute."""
        self.validate()
        tainted = self.tainted_nodes()
        content_bearing = self.content_bearing_nodes()
        cache_unsafe = self.cache_unsafe_nodes()
        lines = [f"graph {self.name!r}: {len(self._nodes)} nodes"]
        for depth, group in enumerate(self.levels()):
            lines.append(f"  level {depth}:")
            for node_id in group:
                node = self._nodes[node_id]
                marks = "".join(
                    f"  [{label}]"
                    for label, present in (
                        ("tainted", node_id in tainted),
                        ("content-bearing", node_id in content_bearing),
                        ("cache-unsafe", node_id in cache_unsafe),
                    )
                    if present
                )
                ports = ", ".join(
                    f"{p}<-{u}" for p, u in sorted(self._wiring[node_id].items())
                )
                wiring = f"  ({ports})" if ports else ""
                lines.append(
                    f"    {node_id:<24} {node.kind.value:<10} v{node.version}"
                    f"{wiring}{marks}"
                )
        return "\n".join(lines)
