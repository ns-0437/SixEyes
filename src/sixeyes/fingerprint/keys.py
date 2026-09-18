"""The local fingerprinting secret.

An *unkeyed* rolling hash chain (`chain[i] = H(chain[i-1], token_i)`) is not safe to export:
anyone holding two adjacent exported digests can test low-entropy candidate words directly
against the public hash function -- `H(prev, "asthma") == next` either matches or it
doesn't, and no cryptographic break is needed to run that test. A confirmed regression
recovered a synthetic sensitive word this way from fingerprint output alone. This is the
fix: every chain step is HMAC'd with a secret that is generated locally, stored locally,
and never included in anything exported, cached to a shared location, logged, or emitted
in a report, Finding, or DivergenceReport. Without the key, the same dictionary test is
infeasible -- the attacker cannot even evaluate the candidate hash to compare.

This does not, on its own, make the chain safe to *share across customers or machines* --
two parties would need the same key to usefully compare fingerprints, which reintroduces
a real key-distribution problem the moment cross-workload or hosted comparison is built.
That is explicitly out of scope here: this fix only restores the claim that content
computed and read on one machine cannot be dictionary-attacked from the fingerprint alone.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

KEY_LENGTH = 32
DEFAULT_KEY_PATH = Path.home() / ".sixeyes" / "fingerprint.key"

_LOCK_SUFFIX = ".lock"
_LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_INTERVAL = 0.02
_READ_WAIT_SECONDS = 0.5


class KeyCreationTimeoutError(TimeoutError):
    """Another process or thread held the key-creation lock for longer than expected."""


def _read_existing(path: Path) -> bytes | None:
    """Read-then-validate, via a direct read attempt rather than a separate `exists()`
    check -- collapsing "check" and "read" into one filesystem call avoids a TOCTOU window
    of its own between the two, and matters for load_or_create_key's retry loop below,
    which calls this repeatedly while waiting on another caller's in-progress creation."""
    deadline = time.monotonic() + _READ_WAIT_SECONDS
    while True:
        try:
            data = path.read_bytes()
            break
        except FileNotFoundError:
            return None
        except PermissionError:
            # Windows can briefly deny reads during atomic installation. An unreadable
            # key is never treated as absent: persistent denial must propagate.
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_POLL_INTERVAL)
    if len(data) != KEY_LENGTH:
        raise ValueError(
            f"fingerprint key at {path} is {len(data)} bytes, expected {KEY_LENGTH} -- "
            "refusing to silently regenerate over what may be an intentional file"
        )
    return data


def load_or_create_key(path: Path = DEFAULT_KEY_PATH) -> bytes:
    """Load the local fingerprinting key, generating one atomically on first use.

    Regression (independent follow-up review, 2026-09-16): the previous check-then-write
    sequence let two concurrent first-use callers both observe absence, each generate a
    *different* secret, and race to overwrite the same path -- whichever wrote last won,
    silently, and the loser's in-memory key no longer matched what was on disk. Fixed with
    a lock file created via O_CREAT|O_EXCL (atomic, cross-platform) as the actual mutual
    exclusion primitive, and the key itself installed via write-to-temp-then-`os.replace`
    (also atomic on POSIX and Windows) so no reader ever observes a partially-written key.

    Best-effort file permission restriction (0600) is applied where the platform supports
    it; this is not a substitute for OS-level secret storage, and a future hosted/
    multi-tenant deployment needs a real secrets manager, not a home-directory file.
    """
    existing = _read_existing(path)
    if existing is not None:
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + _LOCK_SUFFIX)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    while True:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Someone else is creating the key right now -- wait for them to finish and
            # install it, rather than racing to create our own.
            existing = _read_existing(path)
            if existing is not None:
                return existing
            if time.monotonic() > deadline:
                raise KeyCreationTimeoutError(
                    f"timed out waiting for concurrent fingerprint key creation at {path}"
                ) from None
            time.sleep(_LOCK_POLL_INTERVAL)
            continue

        try:
            # Re-check under the lock: another caller may have finished between our first
            # read attempt above and acquiring this lock.
            existing = _read_existing(path)
            if existing is not None:
                return existing

            key = secrets.token_bytes(KEY_LENGTH)
            tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp_path.write_bytes(key)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass  # best-effort; not all platforms support POSIX permission bits
            os.replace(tmp_path, path)  # atomic install -- no reader sees a torn write
            return key
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                pass
