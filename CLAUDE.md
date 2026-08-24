# CLAUDE.md — SixEyes

Read this before touching anything. These 12 rules are load-bearing. Violating one is a
product failure, not a style disagreement.

## What this is

SixEyes is a **read-only forensic analyzer for agentic LLM waste**. It ingests execution
traces, finds spend that is recoverable **with zero change to model behaviour**, localises
the exact cause, and prescribes the fix. Opening capability: prompt-cache hit-rate
engineering. It is not a gateway, not a proxy, not a router.

**North-star metric:** recoverable input cost, verified after the customer ships the fix.

---

## The 12 rules

**1. Quality-neutral first, always.**
We only ship optimisations that produce byte-identical model behaviour until real eval
infrastructure exists. Prompt-cache hits, deduplicated context, eliminated redundant calls.
The moment a capability trades quality for cost, it goes behind an eval gate or it does not
ship. This is the reason we can sell without an eval set, and it is our single biggest
structural advantage. Do not spend it casually.

**2. Determinism is the product.**
Detection is a pure function of the trace. Same input, same findings, forever. No LLM is
ever in the detection path. `NodeKind.STOCHASTIC` nodes are barred by the executor from
producing verified findings — this is enforced in code, not convention. If you find
yourself reaching for a model to decide whether something is waste, you have misunderstood
the product.

**3. Content-free by construction.**
We never store, transmit, or log raw prompt text, completions, tool arguments, or document
content. We handle token-boundary hashes, counts, digests, and structural fingerprints
only. Every field that crosses the collector boundary must be provably non-reversible.
A change that puts customer content into a payload is rejected on sight, no matter how
useful. Our pitch is "we never see your prompts, we see the shape of them" — that sentence
must remain literally true.

**4. Every capability is a node.**
New detection, new normalisation, new economics: a new node with declared typed ports.
Never a new `if` branch inside an existing node. Nodes are independently testable,
independently cacheable, independently parallelisable, and independently deletable. If a
change requires editing three existing nodes, the decomposition is wrong.

**5. Findings carry evidence, remediation, and money.**
A `Finding` without (a) machine-checkable evidence, (b) a concrete remediation, and
(c) a recoverable-dollars estimate is not a finding, it is an opinion. Opinions do not
render in reports.

**6. Never claim savings without a verification row.**
Estimates are labelled estimates. Only a before/after measurement across a customer deploy
becomes a claim. The `verification` table is the marketing department and the only asset
that compounds. Fabricated or extrapolated savings numbers are an existential credibility
risk in a market full of vendors quoting listicles.

**7. Money is a type, never a float.**
`Money` holds integer micro-USD. No `float` dollars anywhere — not in the model, not in
aggregation, not in reports. Token counts are `int`. Rounding happens once, at render time.

**8. Provider pricing is versioned data, not code.**
Prices change monthly. They live in dated data files with explicit effective ranges. Every
cost figure records which price-table version produced it, so historical reports stay
reproducible when prices move.

**9. No premature infrastructure.**
Postgres, not ClickHouse. One process, not microservices. No Kubernetes, no queue, no
service mesh, until a real customer's data volume breaks the simple thing. We are optimising
for time-to-first-verified-finding, not for a diagram that impresses people.

**10. Test invariants, not implementations.**
Hashing, cache keys, and graph execution get property-based tests: determinism, order
independence where claimed, order dependence where claimed, collision behaviour. Detectors
get golden-trace fixtures with known planted waste. Refactors must not require rewriting
assertions.

**11. Every feature answers the scope gate.**
"What is the smallest experiment that proves a customer pays for this?" If there is no
answer, it is research, and research does not go in `src/`. The failure mode of this project
is building the platform before validating the wedge — the founding document had 150
capabilities and zero customers. Do not recreate that in code.

**12. Naming and IP hygiene.**
The product name is SixEyes, by founder decision. Trademark clearance across relevant
classes (software/SaaS) has **not** been run — see the standing flag in `docs/PHASES.md`.
No paid brand spend (domain, trademark filing, paid ads) until it is. Regardless of the
company name, no in-universe technique or character names (Domain Expansion, Limitless,
Hollow Purple, etc.) appear anywhere customer-facing — those read as fan content, not a
company, to an enterprise buyer and invite takedown risk the company name alone does not.

---

## Architecture in one paragraph

A typed DAG. `SOURCE` nodes read traces and normalise them into a provider-agnostic model.
`PURE` nodes fingerprint requests into content-free hashes, then run independent detectors
in parallel. Results fan into correlation and economics nodes that attach recoverable
dollars. Only at the very end, in remediation synthesis, do `STOCHASTIC` nodes appear — and
their output is advisory text attached to a finding that was already proven deterministically.
The executor caches node outputs by content hash, so adding a detector re-runs one node
instead of the whole pipeline.

See `ARCHITECTURE.md` for the node graph and `docs/PHASES.md` for the build plan.

## Working agreements

- Python 3.11+. `src/` layout. Type hints are mandatory and checked.
- Dependencies are a liability. Justify every addition in the PR description.
- `pytest` must pass before any phase is called done. No exceptions, no "will fix next phase".
- Phases ship in order. Do not start phase N+1 while phase N has failing tests.
