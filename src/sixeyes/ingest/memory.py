"""A trace bound to one content-bearing source instance."""
from __future__ import annotations
import uuid
from typing import Any, ClassVar
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.types import RawTrace

class InMemoryTraceSource(Node):
    """A SOURCE node wrapping a RawTrace already built in memory -- no file, no network.
    Construction argument: `trace` (RawTrace), held outside mutable node config.
    Content-bearing, same as JsonlSource. Create a new source for a different trace.

    `config_key()` is a random UUID generated once at construction, not `id(self)`. An
    earlier version used `id(self)` as cache identity; an independent review demonstrated
    concretely why that's wrong: CPython reuses an object's memory address after it is
    garbage collected, so a *freshly constructed* source for a brand-new trace can end up
    with the same `id()` a previous, now-dead source had -- and downstream nodes keyed off
    that identity (Fingerprint) then serve the previous trace's cached fingerprints for
    genuinely different content. A `uuid.uuid4()` nonce has no relationship to memory
    layout and is generated fresh per instance, so two sources are never confused even
    under address reuse. It is still not a content hash (hashing raw customer content into
    a cache-key string would itself be a persistence-boundary concern, CLAUDE.md rule 3) --
    it is only guaranteed unique per *instance*, which is what this in-memory node needs:
    it is always constructed fresh with exactly one trace, never rebound to a different one
    after construction.
    """

    kind = NodeKind.SOURCE
    version = "3"  # nonce bound to construction-time trace outside mutable config
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    def __init__(self, node_id: str, *, trace: RawTrace) -> None:
        super().__init__(node_id)
        self._trace = trace
        self._nonce = uuid.uuid4().hex

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        trace = self._trace
        ctx.log("in-memory trace with %d requests, no file or network involved", len(trace.requests))
        return trace

    def config_key(self) -> Any:
        return {"kind": "in_memory_trace_source", "nonce": self._nonce}
