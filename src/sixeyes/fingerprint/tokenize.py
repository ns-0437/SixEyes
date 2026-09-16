"""Text -> token boundaries.

This is a whitespace tokenizer, not a real BPE tokenizer (tiktoken or equivalent). It is
honest about that limitation rather than calling itself "token-boundary" precision it
doesn't have: it proves the localization *mechanism* -- finding the exact point two
requests diverge, without ever storing the text itself -- correctly and deterministically,
offline, with no dependency. Swapping in a real subword tokenizer later is a drop-in
replacement of `tokenize()` and does not change anything about the fingerprinting or
divergence logic built on top of it. Tracked as a near-term item in docs/PHASES.md.
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"\S+")


def tokenize(text: str) -> tuple[str, ...]:
    """Split on whitespace runs. Deterministic, offline, and sufficient to prove that a
    single changed word (a timestamp, a request id) is localized to its exact position."""
    return tuple(_WORD_RE.findall(text))
