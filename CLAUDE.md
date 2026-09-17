# CLAUDE.md — SixEyes

Read this before touching anything. These 12 rules are load-bearing. Violating one is a
product failure, not a style disagreement.

## What this is

SixEyes is a **read-only forensic analyzer for agentic LLM waste**. It ingests execution
traces, diagnoses recoverable spend, localises the exact cause, and prescribes a fix — without
modifying production requests. Opening capability: prompt-cache hit-rate engineering. It is
not a gateway, not a proxy, not a router.

**Competitive reality, checked 2026-09-16:** Anthropic ships a native `cache-diagnosis` beta
API that fingerprints consecutive requests and reports the exact divergence point
(`model_changed` / `system_changed` / `tools_changed` / `messages_changed`), content-free,
for free, at the source. Single-provider, single-request-pair cache-divergence detection is
therefore not a defensible starting claim — a customer can read it off the response object
today. What is *not* covered by any provider-native feature: cross-provider aggregation (a
customer running Claude + OpenAI + Gemini has no unified view), fleet-level dollar-prioritized
reporting over time, and the non-cache waste categories (redundant tool calls, re-sent
context, retry burn). That narrower, honest claim is what this project is actually testing —
see `docs/PHASES.md` for the standing note and the validation plan built around it.

**North-star metric:** recoverable input cost, verified after the customer ships the fix.

---

## The 12 rules

**1. Diagnose without modifying production; never claim more than that proves.**
SixEyes reads traces and reports findings — it never changes a request in flight. That
boundary is real and defensible. What is *not* true, and must never be claimed as true, is
that applying a recommended fix is guaranteed to preserve model behaviour: identical requests
do not guarantee identical generated output, and reordering a tool array or moving a
timestamp to fix a cache miss is a change to what the model sees, not a provably inert one.
Detection is deterministic (rule 2). Whether a specific *applied* fix preserved the
customer's output quality is a separate, unproven claim until it is checked — do not let the
two blur together in code, in docs, or in front of a customer. Every finding and every
reported number carries one of four confidence tiers (`core.types.Confidence`) and is labelled
accordingly, everywhere it's shown:
- `MEASURED` — a before/after verification row exists across a real customer deploy. The
  only tier allowed to be called a "result."
- `DERIVED` — exact arithmetic on observed tokens and a dated price table. Call it a
  calculation, not a result.
- `ESTIMATED` — modelled from a sample or heuristic. Always say "estimated."
- `SPECULATIVE` — advisory only (remediation prose). Never aggregated into a total, never
  presented as a number a customer can bill against.
A capability that trades quality for cost without a way to check the trade ships behind an
eval gate, or it does not ship.

**2. Determinism is the product.**
Detection is a pure function of the trace. Same input, same findings, forever. No LLM is
ever in the detection path. `NodeKind.STOCHASTIC` nodes are barred by the executor from
producing certified findings — enforced unconditionally at the executor (`_detaint` in
graph/executor.py forces `tainted=True` on every Finding a tainted node's output reaches,
whether or not the node called `ctx.certify`), not opt-in convention. An independent
review (2026-09-16) found that omitting `ctx.certify` was sufficient to bypass the
advertised boundary; `ctx.certify` remains useful for a clean path (proper provenance,
loud in-code failure on misuse) but was never itself the safety mechanism. `_detaint`
walks a fixed whitelist of container shapes (`Finding`, `FindingSet`, `tuple`, `list`,
`dict` values) — a follow-up review found `dict` missing, so a STOCHASTIC node returning
`{"finding": ...}` passed through untouched. A node wrapping a Finding in anything outside
that whitelist is not covered and must not be done without extending `_detaint` to match;
this is a deliberate whitelist, not a generic object walker. If you find yourself reaching
for a model to decide whether something is waste, you have misunderstood the product.

**3. Content-free by construction.**
We never store, transmit, or log raw prompt text, completions, tool arguments, or document
content. We handle whitespace-unit hashes, counts, digests, and structural fingerprints
only — "unit" because our tokenizer is whitespace-preserving, not a real provider/BPE
tokenizer yet; never call an offset a "token" position in anything customer-facing until
that changes (docs/PHASES.md tracks it). Every field that crosses the collector boundary
must be provably non-reversible, and per-step chain hashing is HMAC-keyed with a secret
generated and kept local to the machine (`fingerprint.keys`) — an *unkeyed* rolling hash
chain lets anyone holding two adjacent exported digests test low-entropy candidate words
directly against the public hash function, which a 2026-09-16 independent review
demonstrated as a real, reproduced attack, not a theoretical one.

Content-bearing status **propagates downstream by default**: an ordinary node consuming
content-bearing input is itself treated as content-bearing (`Graph.content_bearing_nodes`)
unless it explicitly sets `declassifies = True` — a reviewed, auditable claim, not a
default any node gets by omission. The same review found the opposite (declared-only,
non-propagating) design let a plain passthrough node persist raw content to disk simply by
never setting a flag. `Fingerprint` is presently the only node that declassifies. A
**declassifying node's own errors are still redacted**, separately from its output
classification — a follow-up review (2026-09-16) found that a declassifier failing
mid-execution embedded its raw *input* (still in scope while running, before it produces
its genuinely content-free output) in the exception message, and that message was NOT
redacted, because redaction keyed off the node's output classification, not its input.
The executor now redacts based on whether a node's *input* is content-bearing, which is a
different question from whether its *output* is.

Structural growth tolerance is scoped to `messages` only. Only a growing conversation
history has real evidentiary support for "this is safe" (Anthropic documents it
explicitly); `tools` and `system` are typically static configuration, and any difference
there — including a pure append, like a new instruction added to a system prompt or a
tool added to an empty list — is reported, not silently treated as growth. A first fix
pass applied the growth tolerance to every segment uniformly; a follow-up review caught
that appending to `system` or `tools` silently reported no change.

Every identifier that leaves this boundary is **keyed**, not merely hashed. A customer
request id is exactly the kind of low-entropy, customer-controlled field someone might
put a real identifier into — an *unkeyed* digest of it is dictionary-guessable the same
way an unkeyed chain step was, which a follow-up review demonstrated by recovering a
synthetic identifier from an unkeyed `request_ref`. Fixed by HMAC-keying it the same way
segment chains are keyed. The key itself is versioned via a content-free `key_ref` on
every `RequestFingerprint`, so a cache never serves fingerprints computed under one key
back under a different one, and comparing two fingerprints from different keys raises
`IncomparableFingerprintsError` rather than misreporting a key rotation as a content
change — a first fix pass keyed the chain but left the cache key and the comparison
function both blind to *which* key was used.

A message's end is marked as explicitly as its start. `("message_start", role,
tool_call_id)` alone left the *last* message's content chain with no terminator, so
extending it (`"hello"` → `"hello extra instructions"`, no new message added) was
structurally indistinguishable from a legitimate new turn being appended — both leave the
old chain as a literal prefix of the new one, which the `messages`-only growth tolerance
correctly treats as safe *when it's actually a new message*. A third-round review
demonstrated the edit case being silently reported as no change. Fixed with an explicit
`("message_end", role)` unit after every message's content (`Fingerprint` version 5) —
editing inserts units before that marker and breaks the prefix relationship; a genuine
append still matches the old chain in full, marker included.

Anything a node resolves once and reuses (a file's bytes, a local key) **must live in a
`RunScoped` (graph/run_scoped.py), never a bare instance attribute the node sets once and
trusts forever.** Two independent bugs — `JsonlSource` silently replaying a prior run's
trace after its input file was deleted, and `Fingerprint` caching a mismatched key across
a rotation mid-run — turned out to be the identical mistake: a value cached on `self`
either never got invalidated when refreshing it failed (deleted-file case), or got
resolved separately in `config_key()` and `execute()` with no guarantee the two calls saw
the same thing (key-rotation case). `RunScoped.resolve(compute, force=True)` — called
once, in `config_key()`, which `Graph.cache_keys()` guarantees runs before any node's
`execute()` each run — invalidates the old value *before* attempting the new one, so a
failed refresh can't leave a stale answer looking current; plain `resolve(compute)` in
`execute()` reuses whatever that forced call just produced. Do not reinvent this per node.

**`RunScoped` protects one run against itself, not two overlapping runs against each
other.** A fourth-round review showed the gap directly: run A plans from a file reading
"alpha" and pauses mid-`execute()`; run B, sharing the same node instances, refreshes that
node's `RunScoped` state to "bravo"; run A resumes and silently consumes "bravo" under its
own "alpha" cache identity, and a later run gets that wrong result served back from cache.
Building real per-run isolation (each run holding its own copy of prepared state) is out
of scope for the first version. The fix, per the review's own recommendation, is the
smallest correct one: `graph/run_isolation.py` claims **every node in the graph** —
not just the requested targets' ancestors — *before* `Graph.cache_keys()` runs, and
rejects a run outright with `OverlappingRunError` if another still-in-flight run already
holds one of those instances. It has to be every node, not just the ones this run's
targets depend on: a fifth-round review found that `cache_keys()` computes an identity for
every node in the graph regardless of which targets were requested (a node's key can only
be as trustworthy as its own freshly-computed `config_key()`), so a target-specific call
that only claimed its own ancestor closure — `targets=["other"]` on a graph that also has
an in-flight `source`/`fingerprint` run — could still have `cache_keys()` silently refresh
an unrelated run's `RunScoped` state. The invariant is **no run may prepare or execute an
unclaimed shared node**, and preparation touches the whole graph, so the claim must too.
The claim is released on every exit path — normal return, a raised exception, or the run's
own task being cancelled. Two graphs built with independent node instances are never
blocked by this, including two different `Executor` objects sharing instances (the
registry is keyed by node identity, not by owning `Executor` or `Graph`) — only literally
running the same node objects concurrently is refused. If concurrent use of shared node
instances later becomes a real requirement, that needs prepared state to live in a per-run
plan/context instead of on the node — a bigger design change, not a fix to bolt on here.

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
Only `Confidence.MEASURED` (rule 1) — a before/after measurement across a real customer
deploy — becomes a claim. The `verification` table is the marketing department and the only
asset that compounds. Fabricated or extrapolated savings numbers are an existential
credibility risk in a market full of vendors quoting listicles.

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
