"""Async ready-queue executor.

A node starts the instant its own dependencies resolve, rather than waiting for its whole
topological level to finish. With five detectors of wildly different cost hanging off one
fingerprint node — which is exactly the shape of the analysis graph — level-synchronous
execution would idle on the slowest detector at every step.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from sixeyes.core.errors import GraphExecutionError
from sixeyes.core.ids import Digest
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
    ) -> Any:
        node = graph.node(node_id)
        tainted = node_id in root_ctx.tainted_nodes
        started = time.perf_counter()

        # CLAUDE.md rule 3: a content-bearing node's output never reaches a persistent
        # cache. It is always recomputed rather than risk raw customer content landing on
        # disk between runs. Non-persistent caches (MemoryCache, NullCache) are unaffected
        # -- they die with the process, so passing content through them is safe.
        cache_is_persistent = getattr(self.cache, "persistent", False)
        use_cache = node.kind.is_cacheable and not (node.content_bearing and cache_is_persistent)

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
                        content_bearing=node.content_bearing,
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
            manifest.record(
                NodeRecord(
                    node_id=node_id,
                    kind=node.kind.value,
                    version=node.version,
                    cache_key=cache_key,
                    outcome="failed",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    tainted=tainted,
                    content_bearing=node.content_bearing,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise GraphExecutionError(node_id, exc) from exc

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
                content_bearing=node.content_bearing,
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
