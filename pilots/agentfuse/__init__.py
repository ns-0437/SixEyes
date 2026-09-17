"""One pilot's glue code -- not a SixEyes product feature.

Everything under `pilots/` is scoped to preparing and (once explicitly authorized)
running one specific pilot workload. It is kept out of `src/sixeyes` deliberately: SixEyes
is meant to stay provider- and framework-agnostic, and a hardcoded bridge to one sibling
project's message shape is exactly the kind of one-off that shouldn't leak into the
general product surface. See `agentfuse/bridge.py` for the reasoning on why this also does
not import the `agentfuse` package itself.
"""
