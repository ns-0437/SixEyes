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
from sixeyes.graph.run_scoped import RunScoped
from sixeyes.ingest.types import RawRequest, RawTrace

_HMAC_DIGEST = "sha256"


def _resolve_key(config: dict[str, Any]) -> bytes:
    """The single place execute() and config_key() both resolve a key from -- so a cache
    key computed ahead of a run and the key actually used to fingerprint agree, and an
    explicit key vs. the local default key are never silently conflated."""
    explicit = config.get("key")
    return explicit if explicit is not None else load_or_create_key()


def _key_ref(key: bytes) -> Digest:
    """A content-free identifier of *which* key was used -- never the key itself. Unkeyed
    hashing is safe here specifically because a key is 256 bits of local randomness, not
    customer-supplied content: there is no low-entropy dictionary to guess against, unlike
    `request_ref` below (see its keyed construction) or the original, since-fixed chain
    weakness this whole module exists to avoid repeating."""
    return content_hash("sixeyes.fingerprint.key_ref.v1", key)


def _keyed_request_ref(key: bytes, request_id: str) -> Digest:
    """A keyed reference to a customer-supplied request id.

    An independent review (2026-09-16, follow-up round) demonstrated that the previous
    construction -- an *unkeyed* `content_hash("request_id", request_id)` -- was itself
    dictionary-guessable against a low-entropy id, the exact class of bug per-step HMAC
    chaining was built to close, just reintroduced through a different field. HMAC-keying
    this reference the same way closes it here too.
    """
    msg = b"sixeyes.fingerprint.request_ref.v1:" + canonical_bytes(request_id)
    return Digest(hmac.new(key, msg, _HMAC_DIGEST).hexdigest())


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
        # A message's outgoing tool-call requests (an assistant deciding to call a tool,
        # not that tool's result -- see RawToolCall) are their own tagged, structured
        # units, same treatment as tool definitions above. Onboarding a real OpenAI-style
        # tool-use loop surfaced why this can't be skipped: without it, a message with no
        # text content that only changes *which tool is called or with what arguments*
        # would fingerprint identically to one with no tool calls at all -- a real change
        # silently invisible to the comparison.
        units.extend(
            ("tool_call", call.id, call.name, call.arguments_json)
            for call in message.tool_calls
        )
        # An explicit end marker, not just an implicit "next message_start or end of
        # stream" boundary. Without one, the last message's content chain has no
        # terminator -- editing it (e.g. "hello" -> "hello extra instructions") inserts
        # more "text" units exactly where a legitimately *appended* new message would
        # have gone, and the two were indistinguishable: both left the old chain as a
        # literal prefix of the new one, which is exactly what the append-growth
        # tolerance (fingerprint.types.GROWTH_TOLERANT_SEGMENTS) treats as safe. The end
        # marker makes editing break the prefix relationship instead: the old chain's
        # last token is this marker, and an edit inserts content *before* it shows up at
        # that position, mismatching immediately rather than merely extending past it.
        # A genuine append still matches the full old chain, marker included, then
        # continues with a new message_start -- unaffected.
        units.append(("message_end", message.role))
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
        request_ref=_keyed_request_ref(key, request.request_id),
        key_ref=_key_ref(key),
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

    The key is resolved once per run via `RunScoped`, the same mechanism JsonlSource uses
    for its file snapshot (see graph/run_scoped.py). A third-round review found that even
    after `key_ref` closed the *cache-collision* half of key handling, `config_key()` and
    `execute()` still each called `_resolve_key()` independently -- fine under a stable
    key, but a key rotated in between (the default key file replaced mid-run) left
    `config_key()`'s cache identity and `execute()`'s actual fingerprint output computed
    under different keys, silently mismatched. `RunScoped` forces a fresh resolution in
    `config_key()` (which always runs first each run) and `execute()` reuses that exact
    value -- and because it is invalidated per run, not held on the instance indefinitely,
    a *real* rotation between two separate runs still takes effect on the next one.
    """

    kind = NodeKind.PURE
    version = "6"  # bumped: message_units now also fingerprints tool_calls
    inputs: ClassVar[dict[str, type]] = {"trace": RawTrace}
    output = tuple
    content_bearing = False
    declassifies = True

    def __init__(self, node_id: str, **config: Any) -> None:
        super().__init__(node_id, **config)
        self._key: RunScoped[bytes] = RunScoped()

    def _resolve(self) -> bytes:
        return _resolve_key(self.config)

    async def execute(self, ctx: RunContext, **inputs: Any) -> tuple[RequestFingerprint, ...]:
        trace: RawTrace = inputs["trace"]
        key = self._key.resolve(self._resolve)
        fingerprints = tuple(fingerprint_request(r, key) for r in trace.requests)
        ctx.log("fingerprinted %d requests, content-free", len(fingerprints))
        return fingerprints

    def config_key(self) -> Any:
        # The key itself must never enter a cache key (that would put secret bytes into a
        # value used to name files on disk). key_ref is a content-free *identifier* of the
        # key, not the key -- distinguishing "processed under key A" from "processed under
        # key B" in the cache, which the previous has_explicit_key boolean could not do:
        # two different explicit keys both reported True and collided in the cache,
        # serving key A's fingerprints back under key B (an independent review's follow-up
        # regression demonstrated this directly).
        return {"key_ref": _key_ref(self._key.resolve(self._resolve, force=True))}
