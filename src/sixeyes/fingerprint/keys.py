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
from pathlib import Path

KEY_LENGTH = 32
DEFAULT_KEY_PATH = Path.home() / ".sixeyes" / "fingerprint.key"


def load_or_create_key(path: Path = DEFAULT_KEY_PATH) -> bytes:
    """Load the local fingerprinting key, generating one on first use.

    Best-effort file permission restriction (0600) is applied where the platform
    supports it; this is not a substitute for OS-level secret storage, and a future
    hosted/multi-tenant deployment needs a real secrets manager, not a home-directory file.
    """
    if path.exists():
        data = path.read_bytes()
        if len(data) == KEY_LENGTH:
            return data
        raise ValueError(
            f"fingerprint key at {path} is {len(data)} bytes, expected {KEY_LENGTH} -- "
            "refusing to silently regenerate over what may be an intentional file"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_LENGTH)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort; not all platforms support POSIX permission bits
    return key
