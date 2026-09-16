"""RunScoped: the shared primitive behind two lifecycle fixes (JsonlSource's snapshot,
Fingerprint's key resolution) -- see graph/run_scoped.py for why they turned out to be the
same bug shape.
"""

from __future__ import annotations

import pytest

from sixeyes.graph import RunScoped


def test_first_resolve_computes() -> None:
    scoped: RunScoped[int] = RunScoped()
    calls = []
    assert scoped.resolve(lambda: calls.append(1) or 42) == 42
    assert calls == [1]


def test_second_plain_resolve_reuses_the_cached_value() -> None:
    scoped: RunScoped[int] = RunScoped()
    calls = []

    def compute() -> int:
        calls.append(1)
        return 42

    assert scoped.resolve(compute) == 42
    assert scoped.resolve(compute) == 42
    assert len(calls) == 1


def test_forced_resolve_always_recomputes() -> None:
    scoped: RunScoped[int] = RunScoped()
    values = iter([1, 2, 3])
    compute = lambda: next(values)  # noqa: E731

    assert scoped.resolve(compute, force=True) == 1
    assert scoped.resolve(compute, force=True) == 2
    assert scoped.resolve(compute) == 2  # plain resolve after a forced one reuses it
    assert scoped.resolve(compute, force=True) == 3


def test_a_failed_compute_leaves_nothing_stale_behind() -> None:
    """The core property both real fixes depend on: force=True invalidates the OLD value
    before attempting the new computation, not after -- so if compute() raises, a
    subsequent plain resolve() does not silently return the previous run's answer."""
    scoped: RunScoped[int] = RunScoped()
    assert scoped.resolve(lambda: 1, force=True) == 1

    def failing() -> int:
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError):
        scoped.resolve(failing, force=True)

    assert not scoped.is_resolved

    with pytest.raises(RuntimeError):
        scoped.resolve(failing)  # plain resolve also correctly attempts fresh, not stale


def test_is_resolved_reflects_state() -> None:
    scoped: RunScoped[int] = RunScoped()
    assert not scoped.is_resolved
    scoped.resolve(lambda: 1)
    assert scoped.is_resolved


def test_none_is_a_valid_resolved_value() -> None:
    """The internal _MISSING sentinel must be distinguishable from a legitimately
    resolved None -- a naive `if self._value is None` check would recompute forever."""
    scoped: RunScoped[None] = RunScoped()
    calls = []

    def compute() -> None:
        calls.append(1)
        return None

    assert scoped.resolve(compute) is None
    assert scoped.resolve(compute) is None
    assert len(calls) == 1
