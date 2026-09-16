"""Stable content hashing.

Every cache key, artifact id, and fingerprint in the system funnels through here. The
guarantees that matter:

* Stable across processes, machines, and Python versions (no reliance on ``hash()``,
  which is salted per process).
* Canonical: dicts hash by sorted key, so equal content always yields equal digest.
* Type-tagged: ``1`` and ``"1"`` must not collide, or a config change could silently
  reuse a stale cache entry and we would report last week's findings as this week's.
* Injective: no two distinct inputs may serialise to the same byte string. Encoding is
  length-framed (a fixed-width byte count precedes every sub-value), not delimiter-joined
  -- a NUL-joined scheme lets ``"a\\x00sb"`` and ``("a", "b")`` serialise identically,
  which is a real, exploitable collision, not just a theoretical one. Length framing is
  the standard fix (the same idea as protobuf's length-delimited fields): a byte count
  that must be consumed exactly cannot be faked by content that merely looks similar.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final, NewType

Digest = NewType("Digest", str)

_DIGEST_LEN: Final = 64
_LEN_BYTES: Final = 8  # supports sub-values up to 2**64 bytes; frame overhead is fixed


def _frame(data: bytes) -> bytes:
    """Length-prefix one already-canonical blob so it self-delimits when concatenated
    with others. This is what makes container serialisation injective: a stream of framed
    blobs can only be produced by that exact sequence of blobs, in that exact order."""
    return len(data).to_bytes(_LEN_BYTES, "big") + data


def _canonical(value: Any) -> bytes:
    """Serialise one value to a canonical, type-tagged byte string.

    Scalars return a tag byte plus their own bytes; containers return a tag byte followed
    by their children's *framed* canonical bytes, concatenated. Framing children (rather
    than joining with a delimiter) is what stops ``{"a": 1}`` from being confusable with a
    crafted string, a differently-nested container, or a different split of the same
    total content.
    """
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("utf-8")
    if isinstance(value, float):
        # Floats are permitted in incidental metadata only; never in Money.
        return b"f" + repr(value).encode("utf-8")
    if isinstance(value, str):
        return b"s" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"y" + value
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(_frame(_canonical(item)) for item in value)
    if isinstance(value, (set, frozenset)):
        framed = sorted(_frame(_canonical(item)) for item in value)
        return b"e" + b"".join(framed)
    if isinstance(value, dict):
        pairs = []
        for key in sorted(value, key=lambda k: str(k)):
            pairs.append(_frame(_canonical(key)) + _frame(_canonical(value[key])))
        return b"d" + b"".join(pairs)
    if hasattr(value, "content_key"):
        return b"o" + _frame(_canonical(value.content_key()))
    raise TypeError(
        f"{type(value).__name__} is not canonically hashable; give it a content_key() method"
    )


def canonical_bytes(value: Any) -> bytes:
    """Public entry point for callers (e.g. fingerprint hashing) that need the same
    injective encoding this module uses internally, without reaching into `_canonical`."""
    return _canonical(value)


def content_hash(*parts: Any) -> Digest:
    """Hash any combination of canonically serialisable values.

    Each part is framed independently before hashing (see `_frame`) so
    ``content_hash("a", "b")`` can never collide with ``content_hash("a\\x00sb")`` or any
    other repartitioning of the same bytes across a different number of arguments.
    """
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(_frame(_canonical(part)))
    return Digest(hasher.hexdigest())


def hash_bytes(data: bytes) -> Digest:
    return Digest(hashlib.sha256(data).hexdigest())


def short(digest: Digest | str, length: int = 12) -> str:
    """Truncated digest for logs and report ids. Never for cache keys."""
    return str(digest)[:length]


def is_digest(value: str) -> bool:
    return len(value) == _DIGEST_LEN and all(c in "0123456789abcdef" for c in value)
