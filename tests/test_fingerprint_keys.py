"""fingerprint.keys: the local secret's creation lifecycle.

Everything here is about the file on disk, not about content-freeness of fingerprints
directly -- see test_fingerprint.py for the guarantees the key itself protects.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from sixeyes.fingerprint.keys import KEY_LENGTH, load_or_create_key
from sixeyes.fingerprint import keys


@pytest.mark.parametrize("stored", [b"k" * KEY_LENGTH, b"corrupt"])
def test_transient_read_denial_preserves_existing_key(
    tmp_path: Path, monkeypatch: Any, stored: bytes,
) -> None:
    path = tmp_path / "fingerprint.key"
    path.write_bytes(stored)
    original_read = Path.read_bytes
    attempts = 0

    def read(candidate: Path) -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("simulated sharing violation")
        return original_read(candidate)

    def no_generation(_: int) -> bytes:
        pytest.fail("An existing inaccessible key must never be regenerated")

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(keys.secrets, "token_bytes", no_generation)
    monkeypatch.setattr(keys.time, "sleep", lambda _: None)
    if len(stored) == KEY_LENGTH:
        assert load_or_create_key(path) == stored
    else:
        with pytest.raises(ValueError, match="refusing to silently regenerate"):
            load_or_create_key(path)
    assert attempts == 3
    assert original_read(path) == stored


def test_persistent_read_denial_is_bounded_and_never_regenerates(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    path = tmp_path / "fingerprint.key"
    stored = b"k" * KEY_LENGTH
    path.write_bytes(stored)
    original_read = Path.read_bytes
    clock = iter([0.0, 0.1, 0.6])
    attempts = 0

    def denied(_: Path) -> bytes:
        nonlocal attempts
        attempts += 1
        raise PermissionError("persistent denial")

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr(keys.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(keys.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="persistent denial"):
        load_or_create_key(path)
    assert attempts == 2
    assert original_read(path) == stored
    assert list(tmp_path.iterdir()) == [path]


def test_creates_a_key_on_first_use(tmp_path: Path) -> None:
    path = tmp_path / "fingerprint.key"
    key = load_or_create_key(path)
    assert len(key) == KEY_LENGTH
    assert path.read_bytes() == key


def test_reuses_an_existing_key(tmp_path: Path) -> None:
    path = tmp_path / "fingerprint.key"
    first = load_or_create_key(path)
    second = load_or_create_key(path)
    assert first == second


def test_rejects_a_key_file_of_the_wrong_length(tmp_path: Path) -> None:
    path = tmp_path / "fingerprint.key"
    path.write_bytes(b"too-short")
    with pytest.raises(ValueError):
        load_or_create_key(path)


def test_concurrent_first_use_converges_on_one_stable_key(tmp_path: Path, monkeypatch: Any) -> None:
    """Regression (independent follow-up review, 2026-09-16): the original check-then-
    write sequence let two simultaneous first-use callers both observe absence, each
    generate a *different* secret, and race to overwrite the same path -- the review
    reproduced two threads returning different keys, only one of which survived on disk.
    Fixed with an O_CREAT|O_EXCL lock file as the actual mutual-exclusion primitive and an
    atomic write-then-`os.replace` for installing the key. This deliberately synchronizes
    both threads to the same first-use race window the review's own reproduction used."""
    path = tmp_path / "fingerprint.key"
    barrier = Barrier(2)
    original_exists = Path.exists

    def synchronized_exists(self: Path) -> bool:
        observed = original_exists(self)
        if self == path:
            barrier.wait(timeout=5)
        return observed

    monkeypatch.setattr(Path, "exists", synchronized_exists)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: load_or_create_key(path), range(2)))

    assert results[0] == results[1] == path.read_bytes()


def test_concurrent_first_use_does_not_deadlock_when_run_many_times(tmp_path: Path) -> None:
    """No injected synchronization -- just genuine concurrent first use, repeated, to
    catch a flaky lock/retry implementation that only happens to work under one specific
    interleaving."""
    for trial in range(20):
        path = tmp_path / f"fingerprint-{trial}.key"
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: load_or_create_key(path), range(4)))
        assert len(set(results)) == 1


def test_permission_error_on_lock_is_retried_when_treated_as_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows raises PermissionError while another caller is deleting the lock file. That is
    contention and must be retried, not crash the caller."""
    real_open = keys.os.open
    calls = {"n": 0}

    def flaky_open(path: Any, flags: int, mode: int = 0o777) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(keys, "_LOCK_CONTENTION_ERRORS", (FileExistsError, PermissionError))
    monkeypatch.setattr(keys.os, "open", flaky_open)

    key = load_or_create_key(tmp_path / "fingerprint.key")

    assert len(key) == KEY_LENGTH
    assert calls["n"] == 2


def test_permission_error_on_lock_propagates_when_not_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows a PermissionError is a real problem and must surface immediately."""

    def denied_open(path: Any, flags: int, mode: int = 0o777) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(keys, "_LOCK_CONTENTION_ERRORS", (FileExistsError,))
    monkeypatch.setattr(keys.os, "open", denied_open)

    with pytest.raises(PermissionError):
        load_or_create_key(tmp_path / "fingerprint.key")
