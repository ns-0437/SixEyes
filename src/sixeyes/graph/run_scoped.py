"""A value that must be resolved exactly once per run, and never staler than that.

Two independent, unrelated-looking bugs turned out to be the same shape: `JsonlSource`
caching a file read on `self` indefinitely (so a missing file after a prior successful
run silently served the old bytes back), and `Fingerprint` resolving its HMAC key
separately in `config_key()` and `execute()` (so a key rotated between those two calls
left the node's cache identity and its actual fingerprint output computed under different
keys). Both are instances of one lifecycle rule a node with expensive-or-external state
needs: *refresh once, at the start of a run, and hold that exact value for the rest of the
run -- never longer, never a stale leftover from a run that failed partway through.*

`Graph.cache_keys()` runs entirely before any node's `execute()` begins (see
graph/executor.py), which is what makes this safe: a node's `config_key()` is guaranteed
to be the first thing called on it each run. Call `resolve(..., force=True)` there, and
plain `resolve(...)` everywhere else that needs the same value within that run.
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")

_MISSING = object()


class RunScoped(Generic[T]):
    """Caches the result of `compute()` for the duration of one run.

    `resolve(compute, force=True)` always calls `compute()` and stores the result --
    freshening the value, and critically, *invalidating the old one before attempting the
    new computation* (not after), so a `compute()` that raises leaves this holding nothing
    stale rather than the previous run's answer. Plain `resolve(compute)` returns the
    already-stored value if one exists, computing fresh only if none does yet -- this is
    what lets `config_key()` and `execute()` (or any two call sites within the same run)
    agree on one shared value instead of each independently recomputing and risking a
    mismatch if the underlying source changed in between.
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: object = _MISSING

    def resolve(self, compute: Callable[[], T], *, force: bool = False) -> T:
        if force:
            self._value = _MISSING  # invalidate first -- a failed compute() must not
            # leave the previous run's value looking current
        if self._value is _MISSING:
            self._value = compute()
        return self._value  # type: ignore[return-value]

    @property
    def is_resolved(self) -> bool:
        return self._value is not _MISSING
