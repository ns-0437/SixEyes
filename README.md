# SixEyes

An experimental, read-only analyzer that imports JSONL LLM execution traces and reports
**structural changes** between consecutive requests — the specific field and position
where one request diverged from the one before it. It never modifies a production
request. Cost attribution, additional waste detectors, and broader provider integrations
are planned, not built; customer savings have not yet been validated. See
[What this is, precisely](#what-this-is-precisely) below for exactly what that does and
doesn't mean.

> **Status: pre-MVP, internal, not yet validated on a real workload.** Phase 1 (graph
> substrate) is done. Phase 2 (ingest + content-free fingerprinting) has met its exit
> criterion via the JSONL path and has been through five rounds of AI-assisted code
> review — see [Review history](#review-history) below for exactly what that does and
> doesn't establish. The kill gate that validates the business idea (5 real workloads,
> ≥25% recoverable cost) is *gathered through*, not a precondition for, a narrow
> authorized pilot — see [Running this against a real
> workload](#running-this-against-a-real-workload) for the one currently-supported way to
> do that.

## What this is, precisely

SixEyes reads a JSONL trace export and reports, for each pair of consecutive requests,
whether the model, system prompt, tool definitions, or message history changed — and if
so, exactly which field and at what offset. That is a **structural divergence**, not a
dollar figure and not proof that anything actually cost money: an unchanged request can
still miss a provider's cache if the entry simply expired, and a changed request can
still hit one if the change fell outside the cached prefix. `DivergenceReport` (see the
diagram below) is explicit about this in its own field names — there is no cost field to
misread as more than it is.

Cost attribution (turning a structural finding into a dollar estimate), additional
detectors (redundant tool calls, re-sent context, retry storms), and non-JSONL ingest
(OTel GenAI, native provider SDKs) are on the roadmap (`docs/PHASES.md`) — planned, not
built. No claim is made here about how much spend this class of problem represents for
any particular team; that's an empirical question the kill gate below exists to answer,
not something to assert in advance of running it.

**What it is not:**

- Not a gateway or a proxy. It never sits between your application and a model provider.
- Not a prompt optimizer. It diagnoses; it does not rewrite, compress, or route anything.
- Not a claim that no one else does cache diagnostics — Anthropic ships a native
  [cache-diagnosis API](https://platform.claude.com/docs/en/build-with-claude/cache-diagnosis)
  that does the single-provider, single-request-pair version of this for free. The honest
  gap SixEyes is testing is narrower: cross-provider aggregation, fleet-level
  dollar-prioritized reporting over time, and the non-cache waste categories (redundant
  tool calls, re-sent context, retry storms) that a single-request cache check doesn't
  touch. See `CLAUDE.md` for the full, unvarnished positioning — including what got
  walked back after checking the competitive landscape properly.

## How it works

A typed DAG, not a linear script — because the actual product is a fixed core pipeline
(ingest → fingerprint) feeding a growing, independently-testable set of detectors that
need to run in parallel and re-analyze incrementally.

```
Customer trace (JSONL)
        │
        ▼
  ┌─────────────┐
  │  JsonlSource │   SOURCE · content-bearing · never persisted
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  Fingerprint │   PURE · the only node that declassifies
  └──────┬──────┘        content-bearing → content-free
         ▼
  ┌─────────────┐
  │  Divergence  │   PURE · content-free in, content-free out
  └──────┬──────┘
         ▼
   DivergenceReport
   (segment, offset, cache_missed_units — a structural
    finding, not yet a dollar figure or a proof of savings)
```

**The governing rule, enforced by the runtime, not just by convention:** every node
declares a `NodeKind`. A `STOCHASTIC` node (the only place an LLM is ever allowed) has its
output tainted unconditionally — the executor forces this regardless of whether the node
tries to skip it — so an LLM can never back a "certified" finding. Content-bearing status
(does this node's output hold raw prompt text?) propagates downstream by default and can
only be cleared by a node that explicitly, auditably declares `declassifies = True`. See
`ARCHITECTURE.md` for the full node-kind table and `CLAUDE.md` for the 13 working rules
this is built against.

## Why this has been reviewed four times

Before this touches a real customer's prompts, an independent adversarial review ran
against the codebase four separate times. Each round found real, reproducible bugs the
previous round's tests didn't catch — an unkeyed hash chain that let low-entropy words be
dictionary-guessed from exported fingerprints, a passthrough node that could silently
persist raw content, a certification bypass, a cache-staleness bug across overlapping
runs. All of them are fixed, and all of them are now permanent regression tests in this
repo (`tests/`), not just a note in a changelog.

This isn't a footnote — it's the actual argument for why the content-freeness claim is
credible: it's been attacked four times and is still standing, not just asserted once and
left alone. The full history is in `docs/PHASES.md`, including what each round found and
exactly which test file guards against it now. The honest caveat sits right next to it: a
fifth round finding something is more likely than not, and "verified" here means "verified
against what's been checked so far," not "complete."

## Project layout

```
sixeyes/
├── CLAUDE.md              # 13 working rules — read this first if you're touching code
├── ARCHITECTURE.md        # node-kind semantics, the graph engine, why it exists
├── docs/PHASES.md         # the 6-phase build plan and full review history
├── src/sixeyes/
│   ├── core/              # Money (int micro-USD), content hashing, Finding/Evidence types
│   ├── graph/             # the typed DAG engine: Node, Graph, Executor, RunScoped,
│   │                      #   run isolation — owned rather than an LLM-agent framework,
│   │                      #   because detection here is deterministic by design
│   ├── ingest/            # trace importers — JSONL (zero-instrumentation) shipped;
│   │                      #   OTel GenAI / native SDK adapters not yet
│   └── fingerprint/       # the content-free fingerprinting + divergence mechanism
└── tests/                 # 139 tests, including permanent regressions for every
                           #   finding from all four review rounds
```

## Running it

```bash
python -m venv .venv
source .venv/Scripts/activate   # or .venv/bin/activate on POSIX
pip install -e ".[dev]"
pytest -q
mypy
```

No external dependencies at runtime — deliberately. See `CLAUDE.md` rule 9.

## Naming

"SixEyes" is a founder-chosen working name. **Trademark clearance has not been run** —
see `docs/PHASES.md`'s standing flag. No paid brand spend (domain, filing, ads) against
this name until it has been.

## License

None chosen yet. Until one is added, standard copyright applies — this code is visible,
not licensed for reuse. Phase 6 of the build plan anticipates an eventual OSS collector
component, but that decision hasn't been made for this repository as a whole.
