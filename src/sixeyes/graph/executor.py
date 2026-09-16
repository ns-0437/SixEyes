"""Async ready-queue executor.

A node starts the instant its own dependencies resolve, rather than waiting for its whole
topological level to finish. With five detectors of wildly different cost hanging off one
fingerprint node — which is exactly the shape of the analysis graph — level-synchronous
execution would idle on the slowest detector at every step.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Any, Iterable

from sixeyes.core.errors import GraphExecutionError
from sixeyes.core.ids import Digest
from sixeyes.core.types import Finding, FindingSet
from sixeyes.graph.cache import ArtifactCache, MemoryCache, is_miss
from sixeyes.graph.context import RunContext
from sixeyes.graph.graph import Graph
from sixeyes.obs.trace import NodeRecord, RunManifest


@dataclass(slots=True)
class RunResult:
    """Outputs plus the audit trail that explains how they were produced."""

    outputs: dict[str, Any] = field(default_factory=dict)
    manifest: RunManifest | None = None

    def __getitem__(self, node_id: str) -> Any:
        return self.outputs[node_id]

    def get(self, node_id: str, default: Any = None) -> Any:
        return self.outputs.get(node_id, default)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.outputs


def _detaint(value: Any, node_id: str) -> Any:
    """Force `provenance.tainted = True` on every Finding reachable inside `value`.

    This is the actual enforcement of CLAUDE.md rule 2, applied unconditionally to every
    output of a tainted node -- not just to findings a node author remembered to pass
    through `ctx.certify`. A STOCHASTIC node returning a hand-built `Finding` (skipping
    `ctx.certify` entirely) previously kept whatever provenance it was constructed with,
    which defaults to `tainted=False` -- meaning `finding.is_certified` could be `True`
    for a finding an LLM produced. `ctx.certify` remains useful for a clean path (it stamps
    proper node_ids and raises loudly, during execution, if a node author calls it on a
    tainted path by mistake) but it was never the actual safety mechanism; this is.

    Recurses through `Finding`, `FindingSet`, `tuple`, `list`, and `dict` (values only --
    keys are not walked; a Finding used as a dict key would be unusual enough to treat as
    unsupported rather than add a second, riskier traversal path for). A 2026-09-16
    follow-up review found `dict` missing -- `{"finding": make_finding()}` from a
    STOCHASTIC node passed through untouched. This is a whitelist of supported output
    shapes, not a generic object walker: a node wrapping a Finding inside some other
    container or a custom class attribute is NOT covered, and must not be done without
    extending this function to match -- an unbounded "walk any object's __dict__" widens
    the surface in ways that are hard to reason about being complete.
    """
    if isinstance(value, Finding):
        if value.provenance.tainted:
            return value
        node_ids = (*value.provenance.node_ids, node_id) if value.provenance.node_ids else (node_id,)
        new_provenance = dataclass_replace(value.provenance, tainted=True, node_ids=node_ids)
        return dataclass_replace(value, provenance=new_provenance)
    if isinstance(value, FindingSet):
        return FindingSet(findings=tuple(_detaint(f, node_id) for f in value.findings))
    if isinstance(value, tuple):
        return tuple(_detaint(v, node_id) for v in value)
    if isinstance(value, list):
        return [_detaint(v, node_id) for v in value]
    if isinstance(value, dict):
        return {k: _detaint(v, node_id) for k, v in value.items()}
    return value


class Executor:
    """Runs a validated graph.

    ``max_concurrency`` bounds simultaneous node execution. Detection nodes are CPU-bound
    over large trace volumes, so the useful ceiling is core count, not thread count — this
    is a guard against thrashing, not a throughput knob.
    """

    def __init__(
        self,
        cache: ArtifactCache | None = None,
        max_concurrency: int = 8,
        fail_fast: bool = True,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.cache: ArtifactCache = cache if cache is not None else MemoryCache()
        self.max_concurrency = max_concurrency
        self.fail_fast = fail_fast

    # ------------------------------------------------------------------ public API

    def run_sync(self, graph: Graph, targets: Iterable[str] | None = None) -> RunResult:
        return asyncio.run(self.run(graph, targets))

    async def run(self, graph: Graph, targets: Iterable[str] | None = None) -> RunResult:
        graph.validate()

        wanted = tuple(targets) if targets is not None else graph.sinks
        if not wanted:
            raise GraphExecutionError("graph", ValueError("graph has no target nodes"))
        for node_id in wanted:
            graph.node(node_id)  # raises GraphBuildError on unknown target

        needed = self._ancestors(graph, wanted)
        keys = graph.cache_keys()
        tainted = graph.tainted_nodes()
        content_bearing = graph.content_bearing_nodes()
        cache_unsafe = graph.cache_unsafe_nodes()

        root_ctx = RunContext(graph_name=graph.name, tainted_nodes=tainted)
        manifest = RunManifest(
            run_id=root_ctx.run_id, graph_name=graph.name, started_at=time.perf_counter()
        )
        result = RunResult(manifest=manifest)

        # Remaining unmet dependencies per node, restricted to the needed subgraph.
        remaining: dict[str, set[str]] = {
            node_id: {u for u in graph.upstreams(node_id).values() if u in needed}
            for node_id in needed
        }
        ready: list[str] = sorted(n for n, deps in remaining.items() if not deps)

        semaphore = asyncio.Semaphore(self.max_concurrency)
        running: dict[asyncio.Task[Any], str] = {}
        failures: list[GraphExecutionError] = []

        try:
            while ready or running:
                while ready and len(running) < self.max_concurrency:
                    node_id = ready.pop(0)
                    task = asyncio.create_task(
                        self._run_node(
                            graph, node_id, keys[node_id], root_ctx,
                            result.outputs, manifest, semaphore,
                            content_bearing, cache_unsafe,
                        ),
                        name=f"sixeyes:{node_id}",
                    )
                    running[task] = node_id

                if not running:
                    break

                done, _ = await asyncio.wait(
                    running.keys(), return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    node_id = running.pop(task)
                    error = task.exception()
                    if error is not None:
                        wrapped = (
                            error
                            if isinstance(error, GraphExecutionError)
                            else GraphExecutionError(node_id, error)
                        )
                        failures.append(wrapped)
                        if self.fail_fast:
                            raise wrapped
                        continue

                    result.outputs[node_id] = task.result()
                    for downstream in graph.dependents(node_id):
                        if downstream in remaining:
                            remaining[downstream].discard(node_id)
                            if not remaining[downstream]:
                                ready.append(downstream)
                    ready.sort()
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            manifest.finished_at = time.perf_counter()

        if failures and not self.fail_fast:
            raise failures[0]
        return result

    # ------------------------------------------------------------------ internals

    async def _run_node(
        self,
        graph: Graph,
        node_id: str,
        cache_key: Digest,
        root_ctx: RunContext,
        outputs: dict[str, Any],
        manifest: RunManifest,
        semaphore: asyncio.Semaphore,
        content_bearing_nodes: frozenset[str],
        cache_unsafe_nodes: frozenset[str],
    ) -> Any:
        node = graph.node(node_id)
        tainted = node_id in root_ctx.tainted_nodes
        is_content_bearing = node_id in content_bearing_nodes
        # Distinct from is_content_bearing (the node's *effective output* classification,
        # which is False for a declassifying node like Fingerprint by design): this is
        # whether the node's INPUT -- what it actually holds in scope while executing --
        # includes content-bearing data. A declassifying node still processes raw content
        # during execution, before it produces its (genuinely content-free) output; if it
        # raises mid-execution, that raw content can be embedded in the exception message.
        # A 2026-09-16 follow-up review demonstrated exactly this: redacting based on
        # output classification let a failed declassifier's error carry its raw input
        # straight into the manifest, because its *output* was (correctly) classified safe.
        handles_content_bearing_input = node.content_bearing or any(
            up in content_bearing_nodes for up in graph.upstreams(node_id).values()
        )
        started = time.perf_counter()

        # CLAUDE.md rule 3: a content-bearing node's *effective* output (its own, or
        # inherited from upstream -- see Graph.content_bearing_nodes) never reaches a
        # persistent cache. An unrecognised custom cache is treated as persistent (fail
        # closed) rather than assumed safe -- a cache that forgets to declare `persistent`
        # must not be able to earn "safe to write raw content" by omission.
        #
        # Separately: a node downstream of a RESIDENT node has a structural cache key that
        # cannot reflect the RESIDENT's actual runtime output (see
        # Graph.cache_unsafe_nodes) -- it is excluded from caching for the same
        # fail-closed reason, to avoid serving a stale result as if it were current.
        cache_is_persistent = getattr(self.cache, "persistent", True)
        use_cache = (
            node.kind.is_cacheable
            and node_id not in cache_unsafe_nodes
            and not (is_content_bearing and cache_is_persistent)
        )

        if use_cache:
            cached = self.cache.get(cache_key)
            if not is_miss(cached):
                manifest.record(
                    NodeRecord(
                        node_id=node_id,
                        kind=node.kind.value,
                        version=node.version,
                        cache_key=cache_key,
                        outcome="cache_hit",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        tainted=tainted,
                        content_bearing=is_content_bearing,
                    )
                )
                return cached

        kwargs = {
            port: outputs[upstream]
            for port, upstream in graph.upstreams(node_id).items()
        }
        ctx = root_ctx.for_node(node_id)

        try:
            async with semaphore:
                value = await node.execute(ctx, **kwargs)
        except Exception as exc:
            # A content-bearing node's exception message may itself carry raw customer
            # content (a malformed field whose value gets embedded in, say, a ValueError
            # from a type conversion) -- the node should sanitise that at the source, but
            # the manifest is the actual export surface, so it redacts unconditionally as
            # a backstop rather than trusting every current and future node author to
            # remember. The original exception (with its full message) is still raised via
            # GraphExecutionError.cause for local, in-process debugging -- only the
            # persisted/exportable manifest record is redacted.
            error_text = (
                f"{type(exc).__name__} (message suppressed: node handles customer content)"
                if handles_content_bearing_input
                else f"{type(exc).__name__}: {exc}"
            )
            manifest.record(
                NodeRecord(
                    node_id=node_id,
                    kind=node.kind.value,
                    version=node.version,
                    cache_key=cache_key,
                    outcome="failed",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    tainted=tainted,
                    content_bearing=is_content_bearing,
                    error=error_text,
                )
            )
            raise GraphExecutionError(node_id, exc) from exc

        if tainted:
            value = _detaint(value, node_id)

        if use_cache:
            self.cache.put(cache_key, value)

        manifest.record(
            NodeRecord(
                node_id=node_id,
                kind=node.kind.value,
                version=node.version,
                cache_key=cache_key,
                outcome="executed",
                duration_ms=(time.perf_counter() - started) * 1000,
                tainted=tainted,
                content_bearing=is_content_bearing,
            )
        )
        return value

    @staticmethod
    def _ancestors(graph: Graph, targets: Iterable[str]) -> frozenset[str]:
        """The minimal subgraph needed to produce the targets.

        Asking for one detector must not drag in the other four.
        """
        seen: set[str] = set()
        stack = list(targets)
        while stack:
            node_id = stack.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend(graph.upstreams(node_id).values())
        return frozenset(seen)
