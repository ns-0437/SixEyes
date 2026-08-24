"""Content-addressed artifact cache.

Incremental re-analysis is the whole point. A customer's trace export is expensive to
fingerprint; adding a detector must not pay that cost again. Because every node has a
content-derived cache key, a changed node invalidates exactly itself and its dependents.

Only ``SOURCE`` and ``PURE`` nodes are cached. ``RESIDENT`` nodes own their own state, and
``STOCHASTIC`` nodes are non-deterministic by definition — caching them would let one
sampled LLM answer masquerade as a stable result.
"""

from __future__ import annotations

import pickle
import shutil
import threading
from pathlib import Path
from typing import Any, Protocol

from sixeyes.core.ids import Digest

_MISS = object()


class ArtifactCache(Protocol):
    """Storage for node outputs keyed by content hash."""

    def get(self, key: Digest) -> Any: ...
    def put(self, key: Digest, value: Any) -> None: ...
    def has(self, key: Digest) -> bool: ...


class NullCache:
    """Disables caching. Used by tests that assert a node actually executed."""

    def get(self, key: Digest) -> Any:
        return _MISS

    def put(self, key: Digest, value: Any) -> None:
        return None

    def has(self, key: Digest) -> bool:
        return False


class MemoryCache:
    """Process-local cache. Default for single-shot CLI runs."""

    def __init__(self) -> None:
        self._store: dict[Digest, Any] = {}
        self._lock = threading.Lock()

    def get(self, key: Digest) -> Any:
        with self._lock:
            return self._store.get(key, _MISS)

    def put(self, key: Digest, value: Any) -> None:
        with self._lock:
            self._store[key] = value

    def has(self, key: Digest) -> bool:
        with self._lock:
            return key in self._store

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class DiskCache:
    """Persistent local cache.

    Pickle is acceptable here because the cache is written and read by the same trusted
    process on the same machine, and entries are addressed by a hash of their own
    provenance. If artifacts ever cross a trust boundary this must become a schema-checked
    format — a note for whoever builds the server.
    """

    def __init__(self, root: Path | str = ".sixeyes-cache") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._memo = MemoryCache()

    def _path(self, key: Digest) -> Path:
        # Two-level fan-out keeps directory listings usable at millions of entries.
        return self.root / str(key)[:2] / f"{key}.pkl"

    def get(self, key: Digest) -> Any:
        hot = self._memo.get(key)
        if hot is not _MISS:
            return hot
        path = self._path(key)
        if not path.exists():
            return _MISS
        try:
            value = pickle.loads(path.read_bytes())
        except Exception:
            # A corrupt entry is a cache miss, never a crash. Drop it and recompute.
            path.unlink(missing_ok=True)
            return _MISS
        self._memo.put(key, value)
        return value

    def put(self, key: Digest, value: Any) -> None:
        self._memo.put(key, value)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a killed process cannot leave a half-written entry that
        # would later deserialise into silent nonsense.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(path)

    def has(self, key: Digest) -> bool:
        return self._memo.has(key) or self._path(key).exists()

    def clear(self) -> None:
        self._memo.clear()
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)


def is_miss(value: Any) -> bool:
    return value is _MISS
