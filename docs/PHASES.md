# Build plan — 6 phases

One phase at a time. A phase is done when its tests pass and its exit criterion is met.
No starting phase N+1 with phase N red.

**Open flag — naming:** the company is called SixEyes by founder decision (2026-08-24).
Trademark clearance (USPTO TESS, IP India, EUIPO TMview, WIPO Global Brand Database,
classes 9 and 42) has not been run. No domain purchase, trademark filing, or paid
marketing spend against this name until clearance completes. This does not block
engineering — it blocks spend.

---

## Phase 1 — Graph substrate  ← CURRENT

Typed DAG engine, domain primitives, content-addressed cache, async executor, run manifest.

**Why first:** every later phase is nodes on this graph. Building detectors first and
retrofitting a graph is the rewrite that kills the timeline.

**Exit criterion:** a graph of ≥5 nodes with fan-out executes in parallel, caches correctly,
re-runs incrementally when one node's version bumps, rejects cycles and type mismatches at
build time, and refuses to certify findings tainted by a `STOCHASTIC` node.

## Phase 2 — Ingest & content-free fingerprinting

OTel GenAI semconv reader, Anthropic/OpenAI SDK adapters, JSONL importer, provider-agnostic
normalisation. Then the crown jewel: token-boundary prefix hashing that localises divergence
without ever holding prompt text.

**Exit criterion:** given two requests differing by one injected timestamp, we report the
divergence offset correctly, and a byte-level audit of the emitted payload proves zero
recoverable customer content.

## Phase 3 — Detectors

`PrefixDivergence`, `ToolDefDrift`, `ContextResend`, `RedundantToolCall`, `RetryBurn`.
Golden trace fixtures with planted waste.

**Exit criterion:** each detector finds its planted defect at 100% recall on fixtures and
produces zero findings on a clean trace.

## Phase 4 — Economics & findings

Versioned provider price tables, recoverable-dollar attribution, correlation to avoid
double-counting overlapping causes, ranking, confidence bands.

**Exit criterion:** total attributed recoverable spend never exceeds actual spend, and
overlapping causes are attributed once.

## Phase 5 — Remediation synthesis (the reviewer graph)

Below the determinism boundary. Worker drafts the fix, N reviewers critique in parallel,
synthesiser merges, pass/fail gate loops back. Dynamic fan-out lands here.

**Exit criterion:** remediation text for a known defect is accurate and specific, and every
stochastic artifact is correctly tainted in the manifest.

## Phase 6 — Delivery

CLI, report renderer, CI regression check, packaged collector, verification recording.

**Exit criterion:** a stranger runs one command against their own trace export and gets a
ranked report with dollar figures. This is the artifact that closes the first design partner.

---

## The kill gate sits between Phase 3 and Phase 4

Once detectors work, run them against 5 real workloads before building economics.
If fewer than 3 show ≥25% recoverable input cost, stop. The rest of the roadmap is
predicated on that number being real.
