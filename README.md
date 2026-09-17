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

A typed DAG, not a linear script — because the intended product is a fixed core pipeline
(ingest → fingerprint) feeding a set of detectors that need to run in parallel and
re-analyze incrementally as they're added. Today that set is exactly one node
(`Divergence`); the pipeline below is what's actually built and tested, not a preview of
detectors that don't exist yet.

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
    finding. `CLAUDE.md` rule 1 defines four confidence
    tiers for any number derived from this later —
    measured / derived / estimated / speculative — and
    nothing produced today is above "estimated.")
```

**The governing rule, enforced by the runtime, not just by convention:** every node
declares a `NodeKind`. A `STOCHASTIC` node (the only place an LLM is ever allowed) has its
output tainted unconditionally — the executor forces this regardless of whether the node
tries to skip it — so an LLM can never back a "certified" finding. Content-bearing status
(does this node's output hold raw prompt text?) propagates downstream by default and can
only be cleared by a node that explicitly, auditably declares `declassifies = True`. See
`ARCHITECTURE.md` for the full node-kind table and `CLAUDE.md` for the 13 working rules
this is built against.

## Review history

SixEyes has undergone iterative AI-assisted code reviews with reproducible regression
tests — five rounds so far, each run independently against the actual codebase. These
reviews identified and corrected privacy and correctness defects: an unkeyed hash chain
that let low-entropy words be dictionary-guessed from exported fingerprints, a passthrough
node that could silently persist raw content, a certification bypass, and two separate
run-isolation bugs across overlapping executions (the second of which was a gap in the
first one's own fix, found the same day it shipped). Every finding is fixed and is now a
permanent regression test in `tests/`, not just a changelog entry.

**Passing tests establish specific tested properties; they do not constitute a formal
security audit or prove universal privacy or correctness.** Known limitations and any
unresolved findings are tracked in `docs/PHASES.md` alongside the implementation as they're
found, not smoothed over once a round's fixes ship. Two rounds out of five found a gap in
the immediately preceding round's own fix — there is no basis here for assuming a sixth
round would find nothing, and "verified" means verified against what has actually been
checked so far, not complete.

## Running this against a real workload

The kill gate in `docs/PHASES.md` (5 real workloads, ≥25% recoverable cost) is the
evidence this whole idea stands or falls on — it has to come from somewhere, and that
somewhere is a controlled pilot, not a precondition that has to be satisfied before any
pilot can happen. This section defines what "authorized" means for that pilot; it is not
an invitation to run this against arbitrary customer data without one.

SixEyes has no network access and no way to reach a provider account or live production
traffic — it only ever reads a JSONL file someone hands it. That narrows the risk but
does not remove the judgment call. A run against real data is authorized only when:

1. **The workload owner explicitly consents** to exporting that specific data for this
   specific purpose, having read this README and `CLAUDE.md` and understood the actual
   scope of what's guaranteed (structural divergence only — no cost claim, no proof of
   savings, no formal audit behind the privacy properties).
2. **Runs are one at a time, on one machine, sequentially.** The run-isolation guard
   (`ARCHITECTURE.md`) rejects overlapping runs that share node instances — it refuses
   the unsafe case, it does not make concurrent runs against shared state safe. Don't try
   to parallelize a pilot across the same graph/node instances.
3. **Findings are reported as what they are.** Nothing produced today clears the
   "measured" confidence tier (see the diagram above) — present results to the workload
   owner as structural findings, not as a savings number, until there's a `verification`
   row to back one.

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
└── tests/                 # 140 tests, including permanent regressions for every
                           #   finding from all five review rounds
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

None chosen yet. Because this repository is public, GitHub's Terms of Service let anyone
view and fork it — that much is unavoidable once a repo is public, license or not. What
that does *not* grant is the reproduction, distribution, or derivative-works rights
associated with an actual open-source license: standard copyright still applies to
everything beyond viewing and forking. (See [GitHub's own licensing
guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).)
Phase 6 of the build plan anticipates an eventual OSS collector component, but a license
decision for this repository as a whole hasn't been made.
