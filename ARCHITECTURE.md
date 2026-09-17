# SixEyes — Architecture & Code Structure

## Why a graph engine at all

The analyzer is not a pipeline, it is a dependency graph with heavy fan-out:

- Detectors are mutually independent and must run in parallel over large trace volumes.
- Adding a detector must not re-run fingerprinting over 2M spans.
- Detection must be reproducible, so node outputs are content-addressed and cacheable.
- Some capabilities hold state across runs (fingerprint indexes); most are pure functions.
- Type errors must surface at graph-build time, not forty minutes into a run.

A hand-rolled `for` loop over detectors gives us none of that. LangGraph gives us the wrong
half of it — it is built for LLM agent control flow, where we are running deterministic
compute over structured data and explicitly banning LLMs from the hot path. So: a small
typed DAG executor we own, roughly 600 lines, zero external dependencies.

## NodeKind — where strategy is enforced by the type system

| Kind | Determinism | Cacheable | May produce certified findings |
|---|---|---|---|
| `SOURCE` | by declared fingerprint | yes | yes |
| `PURE` | total | yes | yes |
| `RESIDENT` | stateful across runs | never (see below) | yes |
| `STOCHASTIC` | none | never | **no** |

The executor taints every artifact downstream of a `STOCHASTIC` node, unconditionally --
`_detaint` forces `tainted=True` onto every `Finding` reachable in a tainted node's output,
whether or not the node called `ctx.certify` (a 2026-09-16 review found omitting it was
enough to bypass the boundary as previously built). This is rule 2 of `CLAUDE.md` compiled
into the runtime: an LLM cannot silently enter the detection path, because the graph
refuses to certify the result regardless of what the node itself does.

`RESIDENT` vs ephemeral is the distinction from the reference architecture: resident nodes
own long-lived state (indexes, baselines) and are addressed by identity; ephemeral nodes are
constructed per run and are addressed by content. Both `RESIDENT` and `STOCHASTIC` nodes'
cache keys are structural (node identity), computed once before any node executes -- that
key cannot reflect that either kind's *actual output* may differ between two separate
`Executor.run()` calls sharing one cache. `Graph.cache_unsafe_nodes()` therefore excludes
both kinds, and everything downstream of either, from caching entirely, rather than risk
serving a stale result -- a first fix pass covered only `RESIDENT`; a follow-up review
demonstrated the identical bug for a `PURE` node downstream of `STOCHASTIC`. A real fix
(cache keys that incorporate a node's actual runtime output) is not yet built.

**Content-bearing propagation.** Orthogonal to `NodeKind`: a node's *effective*
content-bearing status (`Graph.content_bearing_nodes()`) is its own declared
`content_bearing`, OR any upstream's effective status -- propagating downstream by
default, the same way taint does. Only a node that sets `declassifies = True` (presently
just `Fingerprint`) resets that closure; everything else inherits sensitivity rather than
losing it by omission.

**RunScoped: the once-per-run value cache.** A node sometimes needs to resolve something
expensive-or-external once (a file's bytes, a local secret key) and use that exact value
both when `config_key()` computes its cache identity and later when `execute()` actually
runs. A bare instance attribute for this is a trap two different nodes fell into
independently: `JsonlSource` silently replayed a prior run's trace after its file was
deleted (the failed refresh never cleared the old attribute), and `Fingerprint` cached a
key-A cache-identity against key-B fingerprint output when the local key rotated between
the two calls. `graph/run_scoped.py`'s `RunScoped` is the fix, used by both:
`resolve(compute, force=True)` in `config_key()` (guaranteed to run first every
`Executor.run()`, since `Graph.cache_keys()` completes before any node executes)
invalidates the old value *before* attempting the new one, so a failed refresh can't leave
stale data looking current; plain `resolve(compute)` in `execute()` reuses whatever that
forced call just produced, so the two calls provably agree within one run, while a real
change between two *separate* runs on a reused node instance still takes effect on the
next one.

**Run isolation: rejecting overlapping runs, not solving concurrent ones.** `RunScoped`
provably agrees with itself *within* one run; it says nothing about *two overlapping
runs* sharing the same node instances. A fourth-round review reproduced exactly that: run
A pauses mid-`execute()` after planning from "alpha"; run B, sharing the same node
instances, refreshes them to "bravo"; run A resumes and silently consumes "bravo" under
its own "alpha" cache identity. Real per-run isolation (each run holding its own copy of
prepared state, not shared on the node) is the actual fix and is deliberately out of scope
for now. `graph/run_isolation.py` instead refuses the overlap outright: `Executor.run()`
claims **every node in the graph** before `Graph.cache_keys()` can trigger any `RunScoped`
refresh, and raises `OverlappingRunError` if another still-in-flight run already holds one
of them. It must be every node, not just the requested targets' ancestor closure: a
fifth-round review found that `cache_keys()` computes an identity -- and therefore calls
`config_key()` -- for every node in the graph regardless of which targets were asked for,
so a target-specific run (`targets=["other"]`) that claimed only its own ancestors could
still have `cache_keys()` silently refresh an unrelated, still-in-flight run's `RunScoped`
state elsewhere in the same graph. The invariant this enforces is "no run may prepare or
execute an unclaimed shared node," and preparation (`cache_keys()`) touches the whole
graph, so the claim has to as well. The claim registry is a `weakref.WeakSet` keyed by
node *instance* identity -- not owned by a particular `Executor` or `Graph` -- so two
different `Executor` objects sharing node instances are caught too, while two independent
graphs built with independent node instances are never blocked. Released on every exit
path: normal return, a raised exception, or the run's task being cancelled.

## The analysis graph

```
                              ┌──────────────┐
                              │ TraceSource  │  SOURCE
                              └──────┬───────┘
                                     ▼
                              ┌──────────────┐
                              │  Normalise   │  PURE   provider-agnostic model
                              └──────┬───────┘
                                     ▼
                              ┌──────────────┐
                              │ Fingerprint  │  PURE   content-free hashes
                              └──────┬───────┘
                 ┌───────────────────┼───────────────────┐
                 ▼                   ▼                   ▼
        ┌─────────────────┐ ┌────────────────┐ ┌──────────────────┐
        │ PrefixDivergence│ │ ToolDefDrift   │ │ ContextResend    │   PURE, parallel
        └────────┬────────┘ └───────┬────────┘ └────────┬─────────┘
                 │                  │                   │
                 │   ┌──────────────┴───┐  ┌────────────┴────────┐
                 │   │ RedundantToolCall│  │ RetryBurn           │
                 │   └──────────┬───────┘  └────────┬────────────┘
                 └──────────────┼───────────────────┘
                                ▼
                        ┌───────────────┐
                        │  Correlate    │  PURE   dedupe overlapping causes
                        └───────┬───────┘
                                ▼
                        ┌───────────────┐
                        │  Economics    │  PURE   attach recoverable Money
                        └───────┬───────┘
                                ▼
                        ┌───────────────┐
                        │     Rank      │  PURE   order by recoverable $
                        └───────┬───────┘
                                ▼
              ═══════ determinism boundary ═══════
                                ▼
                        ┌───────────────┐
                        │ RemediationLLM│  STOCHASTIC  (Phase 5)
                        │ worker→review │  advisory prose only
                        └───────┬───────┘
                                ▼
                        ┌───────────────┐
                        │    Report     │
                        └───────────────┘
```

Everything above the determinism boundary is reproducible, free, and defensible in a
technical sales conversation. Everything below is explanation, and is labelled as such.

## Code structure

```
sixeyes/
├── CLAUDE.md                  # the 12 rules — read first
├── ARCHITECTURE.md            # this file
├── docs/
│   └── PHASES.md              # build plan, one phase at a time
├── pyproject.toml
├── requirements-pilot.txt     # pinned optional development integration
├── pilots/agentfuse/          # strict memory-only bridge, actual-adapter synthetic demo,
│                             # selected structural report (not Phase 6 economics)
├── src/sixeyes/
│   ├── core/
│   │   ├── units.py           # Money (integer micro-USD), TokenCount
│   │   ├── ids.py             # stable content hashing primitives
│   │   ├── types.py           # domain model: Request, Segment, Finding, ...
│   │   └── errors.py
│   ├── graph/
│   │   ├── node.py            # Node, NodeKind, Port, typed signatures
│   │   ├── graph.py           # builder, validation, cycle detection, topo order
│   │   ├── cache.py           # content-addressed artifact cache
│   │   ├── context.py         # RunContext, artifact store, taint tracking
│   │   ├── executor.py        # async ready-queue scheduler, bounded concurrency
│   │   ├── run_scoped.py      # RunScoped: resolve-once-per-run, never-stale value cache
│   │   ├── run_isolation.py   # rejects overlapping runs sharing stateful node instances
│   │   └── registry.py        # node registration / discovery
│   ├── obs/
│   │   └── trace.py           # per-node timing, cache hit/miss, run manifest
│   ├── ingest/                # Phase 2 (JSONL ingest shipped; OTel/SDK adapters pending)
│   │   ├── types.py           # content-bearing domain model: RawTrace, RawRequest, ...
│   │   ├── jsonl.py           # zero-instrumentation import path + JsonlSource node
│   │   └── tool_choice.py     # shared strict parser for the supported request control
│   ├── fingerprint/           # Phase 2 (shipped)
│   │   ├── types.py           # content-free: RequestFingerprint, DivergenceReport
│   │   ├── tokenize.py        # word-boundary tokenizer (real BPE tokenizer: near-term)
│   │   ├── keys.py            # local HMAC key: atomic first-use creation
│   │   ├── fingerprint.py     # RawTrace -> content-free fingerprints (Fingerprint node)
│   │   └── divergence.py      # consecutive-fingerprint comparison (Divergence node)
│   ├── detect/                # Phase 3
│   ├── econ/                  # Phase 4
│   ├── remediate/             # Phase 5
│   └── cli/                   # Phase 6
└── tests/
```

## Execution model

The offline pilot follows `InMemoryTraceSource → Fingerprint → Divergence` with
NullCache and an ephemeral key. A small renderer selects report fields after execution;
it does not serialize raw objects or the entire run manifest. Source identity is a
random nonce bound to one construction-time trace. Tool-choice identity is HMAC-keyed
independently of prefix segments; a control change carries no estimated prefix loss.

1. **Build** — nodes registered, ports type-checked, cycles rejected. Fails fast, before I/O.
2. **Plan** — topological levels computed; cache keys derived for every node.
3. **Execute** — ready-queue scheduler. A node runs the instant its dependencies resolve,
   under a global concurrency semaphore. Cache hits skip execution entirely.
4. **Manifest** — every run emits node versions, cache keys, hit/miss, timings, and taint
   provenance. Reproducibility is auditable, which matters when a customer disputes a number.

Cache key = `sha256(node_id ‖ node_version ‖ config_hash ‖ sorted(input_artifact_hashes))`.
Bumping a node's `version` invalidates that node and everything downstream, nothing else.

## Deliberately deferred

- **Dynamic fan-out** (map over a runtime-sized collection). Static fan-out covers every
  Phase 1–4 need; dynamic arrives with the reviewer pool in Phase 5.
- **Distributed execution.** Single process until a real trace volume breaks it.
- **Persistence of artifacts across machines.** Local cache only until there is a server.
