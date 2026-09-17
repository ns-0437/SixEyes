# Build plan — 6 phases

One phase at a time. A phase is done when its tests pass and its exit criterion is met.
No starting phase N+1 with phase N red.

**Open flag — naming:** the company is called SixEyes by founder decision (2026-08-24).
Trademark clearance (USPTO TESS, IP India, EUIPO TMview, WIPO Global Brand Database,
classes 9 and 42) has not been run. No domain purchase, trademark filing, or paid
marketing spend against this name until clearance completes. This does not block
engineering — it blocks spend.

---

## Phase 1 — Graph substrate  ← DONE (exit criterion met)

Typed DAG engine, domain primitives, content-addressed cache, async executor, run manifest.

**Why first:** every later phase is nodes on this graph. Building detectors first and
retrofitting a graph is the rewrite that kills the timeline.

**Exit criterion:** a graph of ≥5 nodes with fan-out executes in parallel, caches correctly,
re-runs incrementally when one node's version bumps, rejects cycles and type mismatches at
build time, and refuses to certify findings tainted by a `STOCHASTIC` node.

## Phase 2 — Ingest & content-free fingerprinting  ← EXIT CRITERION MET; SCOPE ITEMS OPEN

OTel GenAI semconv reader, Anthropic/OpenAI SDK adapters, JSONL importer, provider-agnostic
normalisation. Then the crown jewel: prefix hashing that localises divergence without ever
holding prompt text.

**Exit criterion:** given two requests differing by one injected timestamp, we report the
divergence offset correctly, and a byte-level audit of the emitted payload proves zero
recoverable customer content.

**Shipped so far (2026-09-16, hardened same day after an independent review):**
- `ingest.jsonl` — the zero-instrumentation path: one documented JSON-object-per-line
  schema, no tracing SDK required. This is the front door most real workloads will
  actually use first, per the build plan's GTM notes, so it went first.
- `fingerprint.fingerprint` / `fingerprint.divergence` — three *independently*-seeded,
  HMAC-keyed rolling-hash chains, one per segment, walked in Anthropic's documented
  prefix order (verified against their live docs, 2026-09-16): `tools`, `system`,
  `messages`. Model identity is a separate compatibility digest, checked first and
  independently — not a token inside the prefix. Reports the first divergence as one of
  `model_changed` / `system_changed` / `tools_changed` / `messages_changed` — matching
  Anthropic's own `cache_miss_reason` vocabulary — so a report reads the same regardless
  of whether the underlying signal came from a provider API or a trace export analyzed
  after the fact.
- Content-freeness is enforced by the executor, not just asserted: any node's *effective*
  content-bearing status (propagated downstream by default — see CLAUDE.md rule 3) is
  never written to a persistent cache, and the exit criterion's byte-level audit is a real
  test, not a design claim.
- Full pipeline (`JsonlSource → Fingerprint → Divergence`) proven through the real graph
  executor, including that a version bump on `Divergence` does not force `Fingerprint` to
  re-run.

**Independent review, 2026-09-16 — reproduced and fixed same day.** A review verified
against the code (13 written regressions, all reproduced against commit `c5409ce` before
any fix) found: an unkeyed hash chain that let low-entropy words be recovered by dictionary
guessing against exported digests; content-bearing status that didn't propagate to a
passthrough node; raw content able to leak through unsanitised parse-error messages and
raw request ids; whitespace discarded during tokenization (masking whitespace-only
changes); message-boundary markers a content string could impersonate; tool_call_id
ignored entirely; the wrong prefix order (this doc previously said `model → system → tools
→ messages`); a deletion misattributed to the wrong segment; certification bypassable by
skipping `ctx.certify`; stale results served for a `PURE` node downstream of a `RESIDENT`
one; and an ambiguous hash encoding with a real (non-cryptographic) collision. All 13 are
now covered by permanent tests in this repo's own suite (`test_fingerprint.py`,
`test_content_boundary.py`, `test_taint.py`, `test_graph_cache.py`,
`test_ingest_pipeline_integration.py`, `test_core.py`) — see `CLAUDE.md` rules 1–3 and
`ARCHITECTURE.md`'s NodeKind section for the corrected guarantees.

**Known limitation, tracked not hidden:** `fingerprint.tokenize` splits on whitespace
*and* preserves it as its own unit (fixed 2026-09-16 — it previously discarded whitespace
entirely), but this is still not a real subword/BPE tokenizer. It proves the localization
*mechanism* correctly; a "unit offset" is not yet a literal provider token index, and nothing
in this codebase should call it one. Swapping in a real tokenizer (tiktoken or equivalent)
is a drop-in replacement of `tokenize()` with no change to the fingerprinting or divergence
logic built on top of it — near-term, not blocking, and not to be silently forgotten. Per
the same review: that swap is an assumption, not yet a tested one — treat "drop-in" as a
claim to verify when it actually happens, not before.

**Follow-up review, 2026-09-16 — same day, 10 more found, all fixed.** Verifying the fixes
above surfaced seven further gaps (10 written regressions): structural growth-tolerance
applied to every segment uniformly, so appending new `system` text or a `tool` silently
reported no change (now scoped to `messages` only — the only segment with real evidentiary
support for the tolerance, see `GROWTH_TOLERANT_SEGMENTS`); the HMAC key had no identity of
its own, so a cache didn't distinguish fingerprints computed under different keys, and
`fingerprint.keys`'s check-then-write had a real race between two concurrent first-use
callers (fixed with `key_ref`, an `IncomparableFingerprintsError` on mismatched keys, and
an O_EXCL lock file plus atomic install); the fix for unkeyed chain hashing didn't extend
to `request_ref`, which was still an unkeyed digest and still dictionary-guessable (now
HMAC-keyed the same way); a declassifying node's own execution errors weren't redacted,
because redaction keyed off output classification rather than input (a declassifier still
handles raw input while running, before producing its content-free output); `_detaint`'s
container whitelist was missing `dict`, so `{"finding": ...}` from a STOCHASTIC node
bypassed certification; the RESIDENT-descendant cache-staleness fix didn't extend to
STOCHASTIC, which has the identical problem; and `JsonlSource.config_key()` and `execute()`
read the file independently, so an edit landing between the two reads could bind a
downstream cache entry to content that was never actually parsed (fixed by sharing exactly
one read per run — see `JsonlSource._read_snapshot`). All 10 are now permanent tests
(`test_fingerprint.py`, `test_taint.py`, `test_content_boundary.py`, `test_graph_cache.py`,
`test_ingest_pipeline_integration.py`, `test_fingerprint_keys.py`).

**Third-round review, 2026-09-16 — same day again, 3 more found, all fixed.** Verifying
the follow-up round's fixes surfaced three more, two of them in the fixes themselves:
`JsonlSource`'s own snapshot fix from the follow-up round left a gap where a forced
refresh that failed with `FileNotFoundError` never invalidated the old attribute, so
deleting the input file after a successful run made the next run silently replay the
stale trace instead of failing; `Fingerprint`'s `config_key()` and `execute()` still each
resolved the default local key independently, so a key rotated in between left the node's
cache identity and its actual fingerprint output computed under different keys; and
editing the *last* message in a conversation (`"hello"` → `"hello extra instructions"`,
no new message added) was indistinguishable from appending a genuinely new turn, because
nothing marked where a message's content chain was supposed to end. Fixed respectively
with `RunScoped` (`graph/run_scoped.py`, a new shared primitive — see `ARCHITECTURE.md`),
the same `RunScoped` applied to `Fingerprint`'s key resolution, and an explicit
`("message_end", role)` unit closing each message (`Fingerprint` bumped to version 5).
All 3 are permanent tests (`test_run_scoped.py`, `test_ingest_pipeline_integration.py`,
`test_fingerprint.py`), plus one combined end-to-end test exercising all three fixes
together across a realistic multi-run sequence.

**Still open for this phase:** OTel GenAI semconv reader, native Anthropic/OpenAI SDK
adapters, real-tokenizer swap-in above, a real fix for RESIDENT/STOCHASTIC-descendant cache
staleness (current fix is the safe-but-conservative "never cache it" rather than a cache
key that incorporates actual runtime output).

**Pattern across three review rounds, worth naming rather than repeating silently:** each
round's fixes were correct on their own terms and still left a gap an adversarial re-read
found immediately -- the review process is doing real work here, and there is no
particular reason to assume a fourth pass would find nothing. Treat "verified" as "verified
against what's been checked so far," not "complete," until a review comes back clean.

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
