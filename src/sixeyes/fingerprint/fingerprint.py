"""RawTrace -> content-free fingerprints.

This is the collector boundary. Everything before this node holds raw customer content;
everything from here on is hashes, counts, and offsets. The node is content_bearing=False
even though its *input* is content-bearing, because its *output* -- the only thing that can
be cached, logged, or emitted -- provably is not. It is also the one node in the graph
allowed to *declassify*: see `declassifies` below and graph/node.py.
"""

from __future__ import annotations

import hmac
from typing import Any, ClassVar

from sixeyes.core.ids import Digest, canonical_bytes, content_hash
from sixeyes.fingerprint.keys import load_or_create_key
from sixeyes.fingerprint.tokenize import tokenize
from sixeyes.fingerprint.types import SEGMENT_ORDER, RequestFingerprint, SegmentFingerprint
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.types import RawRequest, RawTrace

_HMAC_DIGEST = "sha256"


def _chain_seed(key: bytes, segment_kind: str) -> bytes:
    return hmac.new(key, b"sixeyes.fingerprint.chain.v1:" + segment_kind.encode(), _HMAC_DIGEST).digest()


def _hash_chain(key: bytes, segment_kind: str, tokens: tuple[Any, ...]) -> tuple[Digest, ...]:
    """A keyed rolling hash chain, seeded independently per segment kind.

    Keying every step with a locally-generated, never-exported secret (see
    fingerprint.keys) is what stops an unkeyed chain's known weakness: without HMAC, anyone
    holding two adjacent exported digests can test low-entropy candidate tokens directly
    against the public hash function and confirm a match -- a real, reproduced attack, not
    a theoretical one. Seeding independently per segment (rather than continuing one chain
    across segments) is what stops an earlier segment's removal from shifting every later
    segment's absolute position and misattributing the resulting mismatch.

    Each token is framed via `canonical_bytes` (the same injective, length-prefixed
    encoding core.ids uses for cache keys) before being mixed in, so a *structured* token
    like `("message_start", "assistant", None)` can never collide with a plain content
    string that happens to contain similar text -- see `fingerprint_request` for why that
    distinction matters.
    """
    chain = []
    prev = _chain_seed(key, segment_kind)
    for token in tokens:
        msg = prev + canonical_bytes(token)
        prev = hmac.new(key, msg, _HMAC_DIGEST).digest()
        chain.append(Digest(prev.hex()))
    return tuple(chain)


def _tools_units(request: RawRequest) -> tuple[Any, ...]:
    # Each tool is one structured, tagged unit -- not a delimiter-joined string -- so
    # reordering tools shows up as a change at the reordered position, and a tool whose
    # description happens to contain another tool's name can't be confused with an actual
    # reordering (canonical_bytes distinguishes tuple *structure*, not just substrings).
    return tuple(("tool", tool.name, tool.description, tool.schema_json) for tool in request.tools)


def _system_units(request: RawRequest) -> tuple[Any, ...]:
    if request.system is None:
        return ()
    return tuple(("text", unit) for unit in tokenize(request.system))


def _message_units(request: RawRequest) -> tuple[Any, ...]:
    units: list[Any] = []
    for message in request.messages:
        # A structural boundary marker, tagged and tuple-shaped -- never a bare string --
        # so message content cannot impersonate a role change. tool_call_id is included so
        # a changed tool-call binding (same text, different call) is itself detected as a
        # change, not silently ignored.
        units.append(("message_start", message.role, message.tool_call_id))
        units.extend(("text", unit) for unit in tokenize(message.content))
    return tuple(units)


_SEGMENT_BUILDERS = {
    "tools": _tools_units,
    "system": _system_units,
    "messages": _message_units,
}


def fingerprint_request(request: RawRequest, key: bytes) -> RequestFingerprint:
    """Build one request's content-free fingerprint.

    `key` must be kept local -- see fingerprint.keys -- and is never itself part of the
    returned fingerprint. Model identity is compared as its own digest, independent of and
    prior to the segment chains: per Anthropic's documented behaviour, a model change
    invalidates cache compatibility outright, rather than occupying a position within the
    tools/system/messages prefix (see fingerprint.types.SEGMENT_ORDER).
    """
    segments = []
    for kind in SEGMENT_ORDER:
        units = _SEGMENT_BUILDERS[kind](request)
        segments.append(
            SegmentFingerprint(kind=kind, unit_count=len(units), chain=_hash_chain(key, kind, units))
        )
    return RequestFingerprint(
        request_ref=content_hash("request_id", request.request_id),
        timestamp=request.timestamp,
        model_digest=content_hash("model", request.model),
        segments=tuple(segments),
    )


class Fingerprint(Node):
    """RawTrace -> tuple[RequestFingerprint, ...], one per request, in trace order.

    The only node permitted to declassify: its input (RawTrace) is content-bearing, its
    output provably is not. Every other node inherits content-bearing status from its
    inputs by default (see graph/node.py, graph/graph.py) -- this is the one deliberate,
    reviewed exception, not a default any node gets by skipping a flag.
    """

    kind = NodeKind.PURE
    version = "2"
    inputs: ClassVar[dict[str, type]] = {"trace": RawTrace}
    output = tuple
    content_bearing = False
    declassifies = True

    async def execute(self, ctx: RunContext, **inputs: Any) -> tuple[RequestFingerprint, ...]:
        trace: RawTrace = inputs["trace"]
        key = self.config.get("key") or load_or_create_key()
        fingerprints = tuple(fingerprint_request(r, key) for r in trace.requests)
        ctx.log("fingerprinted %d requests, content-free", len(fingerprints))
        return fingerprints

    def config_key(self) -> Any:
        # The key itself must never enter a cache key (that would put secret bytes into a
        # value used to name files on disk) -- only *whether* an explicit key was provided
        # affects the key. Since load_or_create_key() is stable across calls on one
        # machine, omitting it from the cache key does not create staleness in practice.
        return {"has_explicit_key": "key" in self.config}
