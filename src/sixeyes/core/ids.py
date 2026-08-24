"""Stable content hashing.

Every cache key, artifact id, and fingerprint in the system funnels through here. The
guarantees that matter:

* Stable across processes, machines, and Python versions (no reliance on ``hash()``,
  which is salted per process).
* Canonical: dicts hash by sorted key, so equal content always yields equal digest.
* Type-tagged: ``1`` and ``"1"`` must not collide, or a config change could silently
  reuse a stale cache entry and we would report last week's findings as this week's.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final, NewType

Digest = NewType("Digest", str)

_DIGEST_LEN: Final = 64
_NULL: Final = b"\x00"


def _canonical(value: Any) -> bytes:
    """Serialise to a canonical, type-tagged byte string.

    The tag prefix is what stops ``{"a": 1}`` and ``[["a", 1]]`` from colliding.
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
        return b"l" + _NULL.join(_canonical(item) for item in value) + b"|"
    if isinstance(value, (set, frozenset)):
        parts = sorted(_canonical(item) for item in value)
        return b"e" + _NULL.join(parts) + b"|"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value, key=lambda k: str(k)):
            parts.append(_canonical(key) + b"=" + _canonical(value[key]))
        return b"d" + _NULL.join(parts) + b"|"
    if hasattr(value, "content_key"):
        return b"o" + _canonical(value.content_key())
    raise TypeError(
        f"{type(value).__name__} is not canonically hashable; give it a content_key() method"
    )


def content_hash(*parts: Any) -> Digest:
    """Hash any combination of canonically serialisable values."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(_canonical(part))
        hasher.update(_NULL)
    return Digest(hasher.hexdigest())


def hash_bytes(data: bytes) -> Digest:
    return Digest(hashlib.sha256(data).hexdigest())


def short(digest: Digest | str, length: int = 12) -> str:
    """Truncated digest for logs and report ids. Never for cache keys."""
    return str(digest)[:length]


def is_digest(value: str) -> bool:
    return len(value) == _DIGEST_LEN and all(c in "0123456789abcdef" for c in value)
