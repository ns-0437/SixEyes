"""Text -> whitespace-preserving units.

This is not a real BPE/subword tokenizer. It proves the localization *mechanism* --
finding the exact point two requests diverge, without ever storing the text itself --
correctly and deterministically, offline, with no dependency. A "unit" here is either a
maximal run of non-whitespace characters or a maximal run of whitespace characters; the
concatenation of a text's units always exactly reconstructs the original string, which is
what makes a whitespace-only edit (`"Be helpful"` vs `"Be  helpful"`) detectable -- an
earlier version split only on non-whitespace runs and silently discarded whitespace,
making the two strings indistinguishable.

A `unit_offset` reported anywhere in this package is an offset into this whitespace-unit
sequence, never a provider/BPE token index -- the two are different units of measurement
and must not be presented as interchangeable. Swapping in a real tokenizer later is a
drop-in replacement of `tokenize()`; it does not change anything about the fingerprinting
or divergence logic built on top of it, but it does change what a unit *means*, so any
report surfacing this value must keep saying "unit," not "token," until that swap happens.
"""

from __future__ import annotations

import re

_UNIT_RE = re.compile(r"\S+|\s+")


def tokenize(text: str) -> tuple[str, ...]:
    """Split into whitespace-preserving units. Deterministic, offline, and exact: joining
    the result with `""` always reconstructs `text` byte-for-byte."""
    return tuple(_UNIT_RE.findall(text))
