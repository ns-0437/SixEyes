"""Reject overlapping runs that would share -- and silently clobber -- a stateful node's
prepared state.

`RunScoped` (run_scoped.py) makes a node's cache-key computation and its actual execution
agree on one value *within* a single run. It says nothing about *two overlapping runs*
touching the same node instance at once: a fourth-round review demonstrated exactly that
-- run A plans from a file reading "alpha" and pauses mid-execution; run B, sharing the
same node instances, refreshes that node's `RunScoped` state to "bravo"; run A resumes and
silently consumes "bravo" under its own "alpha" cache identity, and a later run gets that
wrong result served back from cache.

Building real concurrent-run isolation (each run holding its own copy of prepared state)
is deliberately out of scope for the first version -- per the review's own guidance, the
smallest correct fix is to refuse the overlap outright: claim every node instance a run
will touch before doing any preparation (before `Graph.cache_keys()` runs, since that is
what triggers a `RunScoped` node's forced refresh), release the claim when the run ends
for any reason, and reject a run that would need a node instance another still-in-flight
run already holds. Two independent graphs built with independent node instances are never
blocked by this; only literally sharing node objects across concurrent runs is -- and the
claim registry is keyed by node *instance* identity, not by owning `Executor` or `Graph`,
so this also catches the case of two different `Executor` objects sharing node instances,
which a per-Executor lock would miss.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from sixeyes.graph.node import Node


class OverlappingRunError(RuntimeError):
    """Raised when starting a run would require using a node instance that another,
    still-in-flight run already claimed. Construct fresh node instances (a fresh `Graph`)
    for each concurrent run, or run them one at a time against the same instances."""


_claimed: "weakref.WeakSet[Node]" = weakref.WeakSet()


def claim(nodes: Iterable["Node"]) -> list["Node"]:
    """Claim every node in `nodes` for the calling run, atomically: either all of them are
    free and all get claimed, or none are claimed and the error names the ones already in
    use. No `await` happens between the check and the claim, so this is correct under
    asyncio's cooperative scheduling without a separate lock -- only an `await` point can
    let another coroutine interleave, and there isn't one here.
    """
    nodes = list(nodes)
    busy = [n for n in nodes if n in _claimed]
    if busy:
        names = ", ".join(sorted(n.node_id for n in busy))
        raise OverlappingRunError(
            f"node(s) {names} are already in use by another in-flight run -- "
            "construct fresh node instances per concurrent run, or run them sequentially"
        )
    for n in nodes:
        _claimed.add(n)
    return nodes


def release(nodes: Iterable["Node"]) -> None:
    """Release a prior claim. Safe to call even for nodes that were never actually
    claimed (e.g. a run that raised `OverlappingRunError` before acquiring anything, or a
    partial claim being unwound) -- `discard` is a no-op when the item isn't present."""
    for n in nodes:
        _claimed.discard(n)
