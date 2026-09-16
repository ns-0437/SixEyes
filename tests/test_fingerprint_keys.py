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
