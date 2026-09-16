"""RawTrace -> content-free fingerprints.

This is the collector boundary. Everything before this node holds raw customer content;
everything from here on is hashes, counts, and offsets. The node is content_bearing=False
even though its *input* is content-bearing, because its *output* -- the only thing that can
be cached, logged, or emitted -- provably is not.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sixeyes.core.ids import content_hash
from sixeyes.fingerprint.tokenize import tokenize
from sixeyes.fingerprint.types import RequestFingerprint, SegmentSpan
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.types import RawRequest, RawTrace

_CHAIN_SEED = content_hash("sixeyes.fingerprint.chain.v1")


def _hash_chain(tokens: tuple[str, ...]) -> tuple[Any, ...]:
    chain = []
    prev = _CHAIN_SEED
    for token in tokens:
        prev = content_hash(prev, token)
        chain.append(prev)
    return tuple(chain)


def fingerprint_request(request: RawRequest) -> RequestFingerprint:
    """Build one request's combined, content-free fingerprint.

    Segments are concatenated in provider prefix order (model, system, tools, messages):
    a provider builds a single cacheable prefix in this order, so a change in an earlier
    segment invalidates every later one regardless of what "changed" within it -- which is
    exactly what walking one combined chain, rather than four independent ones, captures.
    """
    segment_tokens: list[tuple[str, tuple[str, ...]]] = [
        ("model", (request.model,)),
        ("system", tokenize(request.system) if request.system else ()),
        (
            "tools",
            tuple(
                f"{tool.name}\0{tool.description}\0{tool.schema_json}"
                for tool in request.tools
            ),
        ),
        (
            "messages",
            tuple(
                token
                for message in request.messages
                for token in (f"role:{message.role}", *tokenize(message.content))
            ),
        ),
    ]

    all_tokens: list[str] = []
    spans: list[SegmentSpan] = []
    for kind, tokens in segment_tokens:
        start = len(all_tokens)
        all_tokens.extend(tokens)
        spans.append(SegmentSpan(kind=kind, start=start, length=len(tokens)))

    return RequestFingerprint(
        request_id=request.request_id,
        timestamp=request.timestamp,
        token_count=len(all_tokens),
        chain=_hash_chain(tuple(all_tokens)),
        spans=tuple(spans),
    )


class Fingerprint(Node):
    """RawTrace -> tuple[RequestFingerprint, ...], one per request, in trace order."""

    kind = NodeKind.PURE
    version = "1"
    inputs: ClassVar[dict[str, type]] = {"trace": RawTrace}
    output = tuple
    content_bearing = False

    async def execute(self, ctx: RunContext, **inputs: Any) -> tuple[RequestFingerprint, ...]:
        trace: RawTrace = inputs["trace"]
        fingerprints = tuple(fingerprint_request(r) for r in trace.requests)
        ctx.log("fingerprinted %d requests, content-free", len(fingerprints))
        return fingerprints
