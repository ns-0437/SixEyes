# Build plan — 6 phases

One phase at a time. A phase is done when its tests pass and its exit criterion is met.
No starting phase N+1 with phase N red.

## Current execution checkpoint — 2026-09-17

**2026-09-18 update:** the founder authorized a public-source AgentFuse task, a maximum
of 10 inference calls, and ultimately a $0 budget (overriding an earlier $1 ceiling).
The local pilot uses Qwen3-1.7B Q8_0 and a pinned llama.cpp CPU build. Its transport
has no paid endpoint or fallback, reserves calls before POST in a persistent counts-only
ledger, and its tools read only the pinned public Git tree. The model runs outside
SixEyes detection, which remains deterministic. See `LOCAL_PILOT.md` for execution,
limits and results. This remains one engineering validation experiment, not customer
savings evidence or completion of the five-workload kill gate.

The offline AgentFuse pilot now accepts the real adapter's tool-use requests rather
than only proving their rejection. `RawToolChoice` preserves supported explicit choices;
Fingerprint v7 HMACs them separately and Divergence v4 reports control changes without
inventing provider-prefix offsets or affected-token estimates. Missing and explicit
default choices are intentionally distinct.

`python -m pilots.agentfuse` runs the actual AgentFuse adapter with synthetic responses
and an in-memory tool router. Stable growth, injected system drift, and a restart after
an established tool turn produce reviewable text/JSON reports. The demo's monitor only
triggers the adapter's restart branch; it does not validate AgentFuse's own detectors.
The drift scenario intentionally changes a captured fixture, not a production request.
See `docs/PILOT.md` for setup and the exact evidence boundary.

Unknown input key names are now redacted as well as invalid values. The source trace
binding lives outside mutable node config. The selected report exports no raw content
or arbitrary workload/model labels. Tests exercise actual adapter behavior, malformed
shapes, report privacy and outbound-connection rejection. CI is configured for Python
3.11/3.12 on Windows/Linux with the AgentFuse revision pinned in requirements-pilot.txt.
Local verification and remote CI results must be reported separately.

**Next:** obtain the owner's task/data scope and execution budget; run one controlled
session locally; ask whether its structural observation is new, actionable and worth
paying to investigate. Any applied fix needs separate quality and observed-usage checks.
Synthetic fixtures are not one of the five real workloads and provide no savings claim.
Do not start Phase 4 economics or expand infrastructure on this evidence alone.

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
normalisation. Then the crown jewel: prefix hashing that localises divergence while
keeping raw prompt text out of anything that persists, is logged, or is exported beyond
the fingerprinting step itself. Raw text *is* held transiently in memory during ingest and
fingerprinting -- `content_bearing` exists precisely to track that -- the guarantee is
about what crosses the collector boundary into a cache entry, a manifest, or a report, not
that the process never touches the text at all.

**Exit criterion:** given two requests differing by one injected timestamp, we report the
divergence offset correctly, and a test demonstrates that several planted secret strings
do not appear in the emitted payload in any form (byte-level, not merely absent as
plaintext). That establishes the specific tested property -- it is evidence toward
content-freeness, not a formal proof that no customer content could ever be recoverable
from any output under any input.

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

**Fourth-round review, 2026-09-17 — one more found, fixed.** `RunScoped` (third round)
guarantees a node's `config_key()` and `execute()` agree *within* one run; it does nothing
for *two overlapping runs* sharing the same node instances. Reproduced: run A plans from a
file reading "alpha" and pauses mid-`execute()`; run B, sharing the same node instances,
refreshes them to "bravo"; run A resumes and silently consumes "bravo" under its own
"alpha" cache identity, and a later run gets that wrong result served back from cache.
Real per-run isolation (each run holding its own copy of prepared state, not shared on the
node) is out of scope for the first version, per the review's own recommendation — the
fix is `graph/run_isolation.py`: `Executor.run()` claims every node instance it will touch
before `Graph.cache_keys()` can trigger any refresh, and rejects the run outright
(`OverlappingRunError`) if another in-flight run already holds one. Released on every exit
path (return, exception, or cancellation) via a `finally` wrapping the whole run. This is
a permanent test (`test_run_isolation.py`, 6 cases) covering the core scenario, cleanup
under cancellation and under a raised exception, and that independent graphs never trip
the guard.

**Fifth-round review, 2026-09-17 — same day, a gap in the fourth round's own fix.**
`claim()` was only called against `needed` -- the requested targets' ancestor closure --
but `Graph.cache_keys()` (called immediately after) computes an identity for *every* node
in the graph, regardless of target, because a node's cache key can only be as trustworthy
as its own freshly-computed `config_key()`. Reproduced: run A targets `fingerprint` on a
graph that also has an unrelated `other` node, plans from "alpha", and pauses
mid-`execute()`; run B targets only `other` -- its claim succeeds, since it never asked
for `source`/`fingerprint` -- but B's `cache_keys()` call still silently refreshes
`source`'s `RunScoped` snapshot to "bravo" on its way to computing `other`'s identity. Run
A resumes holding the wrong content under its own, now-stale cache identity, and a later
run receives the contaminated cached fingerprint. Fixed by claiming every node in the
*graph*, not just the requested targets' ancestors -- the invariant is "no run may prepare
or execute an unclaimed shared node," and preparation (`cache_keys()`) touches the whole
graph regardless of target, so the claim has to as well. One-line fix in `Executor.run()`;
permanent test added to `test_run_isolation.py`.

**Still open for this phase:** OTel GenAI semconv reader, native Anthropic/OpenAI SDK
adapters, real-tokenizer swap-in above, a real fix for RESIDENT/STOCHASTIC-descendant cache
staleness (current fix is the safe-but-conservative "never cache it" rather than a cache
key that incorporates actual runtime output), and real per-run isolation for concurrent
use of shared node instances (current fix rejects the overlap rather than solving it, and
now conservatively claims the whole graph per run rather than only the requested targets).

**Pattern across five review rounds, worth naming rather than repeating silently:** each
round's fixes were correct on their own terms and still left a gap an adversarial re-read
found immediately -- twice now within the *same* fix (the fourth round's overlap guard had
its own gap found the same day, in round five). The review process is doing real work
here, and there is no particular reason to assume a sixth pass would find nothing. Treat
"verified" as "verified against what's been checked so far," not "complete," until a
review comes back clean.

**First pilot, preparation phase (2026-09-17).** Moving toward one controlled pilot on
AgentFuse (a real, actively-maintained agentic tool-use project) surfaced a real ingest
gap before any real data was touched: `RawMessage` had no slot for an assistant's
outgoing `tool_calls`, only for a tool's result. A real OpenAI-style tool-use loop would
have hit that gap immediately -- either silently dropping tool-call data or stringifying
it, both of which could make an actually-changed request compare as unchanged. Fixed
(`RawToolCall`, `Fingerprint` version 6) and tested entirely offline with synthetic data
that mirrors the real shape (no API calls, no real project data) -- see `test_tool_calls.py`.
Explicitly confirmed as part of that work: SixEyes still has no redundant-tool-call
detector, so two genuinely identical tool calls appended as separate turns correctly
report no divergence today. The first real pilot session, once authorized, validates the
ingest/fingerprinting mechanism against real agentic-loop-shaped data and checks whether
the agent's own context stays cache-stable turn to turn -- it does not, and cannot yet,
surface a redundant-tool-call finding.

**Argument-string correction (2026-09-17, same-day follow-up).** Reading AgentFuse's real
adapter (`agentfuse/adapters/openai_sdk.py`) before building the pilot's capture shim
found the `RawToolCall` fix above still had one wrong assumption baked in: it treated a
tool call's `arguments` as a JSON *object* and ran it through `canonical_json` on ingest,
the same treatment given to a tool's static schema. Real tool-call arguments are model
output carried as a raw *string* (OpenAI's SDK types `tool_calls[].function.arguments` as
`str`, not a parsed object), not guaranteed to even be valid JSON -- AgentFuse's own code
defensively handles a model emitting a broken escape sequence. Canonicalizing this field
would silently reformat what the model actually said, and would crash outright on
malformed-but-real input. Fixed: the field is now `arguments_raw`, stored verbatim, never
parsed or re-serialized; the JSONL schema requires `arguments` as a string and rejects
(does not coerce) anything else. This was caught by reading the real integration target's
source before writing fixture data against a guessed shape, not by an external review --
worth noting as the same discipline that produced every other fix in this log.

**Two qualifications worth stating plainly, not just implying:**

- `DivergenceKind.NONE` means "no structural divergence detected under SixEyes's current
  comparison rules." It is not proof that a provider actually served a cache hit for that
  request, and it is not proof that the calls being compared were worthwhile to make in
  the first place -- a provider's cache entry can expire for reasons this analyzer cannot
  observe, and two structurally-identical calls can both still be waste (see the
  redundant-tool-call gap above). Treat `NONE` as "nothing this tool can flag," not as
  "confirmed savings."
- `test_concurrent_first_use_does_not_deadlock_when_run_many_times`
  (`tests/test_fingerprint_keys.py`) failed once during earlier work in this session with a
  `PermissionError` from `pathlib` under apparent Windows file-lock contention, then passed
  on immediate rerun. Its traceback was not captured at the time -- a gap in its own right,
  noted here rather than glossed over -- so it cannot be reproduced from this record. A
  fresh attempt just now (5 consecutive runs of this test alone) did not reproduce it
  either. This is recorded as an open, unresolved observation, not a confirmed non-issue: a
  single unreproduced failure plus a handful of clean reruns rules out "always broken," not
  "never happens under contention." Anyone who hits it again should capture the full
  traceback verbatim and add it here rather than re-running past it silently.

**Bridge review round, three reproduced defects (2026-09-17, second follow-up).** An
independent review of the memory-only capture shim (commit `7bfdf9d`) reproduced 7 failing
regressions against the live repository before any fix, across three real defects:

1. `InMemoryTraceSource.config_key()` used `id(self)` as cache identity. CPython reuses a
   garbage-collected object's address, so a freshly constructed source for a genuinely new
   trace could receive a previous, dead source's `id()` -- and a downstream Fingerprint
   node keyed off that identity would then serve the previous trace's cached segments for
   different content. Fixed: a random `uuid.uuid4()` nonce generated once per instance,
   unrelated to memory layout, replaces `id(self)` in the cache key (`InMemoryTraceSource`
   version bumped to `"2"`).
2. Rejection errors interpolated the actual rejected value (`!r`) for role, tool `type`,
   and tool-call `type` fields. A planted sensitive marker survived verbatim into the
   raised `AgentFuseShapeError` in each case -- a field that *looks* like a small fixed
   enum is still untrusted input until validated, and this error path runs before any
   redaction the executor might otherwise apply. Fixed: every rejection now reports a
   field path and the expected shape/allowed values only, never the value that failed.
3. Three fields disappeared silently instead of being explicitly rejected: `tool_choice`
   (a top-level kwarg the fake client didn't capture at all, so the converter never got a
   chance to see or reject it), a tool function's `strict`, and a message's `name` (both
   present in captured data but silently ignored by the converter reading only recognized
   keys). This contradicted the bridge's own stated contract. Fixed: `CapturedCall` now
   keeps every kwarg outside `model`/`messages`/`tools` in `extra_kwargs`, and the
   converter explicitly rejects any top-level or nested key it has no supported mapping
   for, rather than reading past it.

The same review also flagged a real coverage gap: every existing test exercised
`run_scripted_loop`, SixEyes's own reimplementation of `guarded_tool_loop`'s shape, never
the actual AgentFuse package. `tests/test_agentfuse_real_adapter.py` closes that: it
imports the real `guarded_tool_loop` (opt-in via `AGENTFUSE_PATH`, pointed at a local
checkout -- not a hardcoded path, not a production dependency, skipped by default) and
runs it against the same fake client, confirming that a real captured call with tools
present genuinely does carry `tool_choice` and is genuinely rejected, and that the
adapter's actual "rerun" restart mechanism (`_apply_directive`, triggered here via a
minimal fake monitor rather than real threshold tuning) truncates the captured message
history exactly as documented. Verified locally against the real checkout: 169 tests pass
with `AGENTFUSE_PATH` set (166 plus these 3; they skip without it), mypy strict is clean
across 28 source files (`src/sixeyes` and `pilots`) -- the mypy verification an external
review's own environment (Python 3.12 with a Python-3.11-only compiled mypy dependency)
could not perform.

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

**What this threshold does and does not establish.** ≥25% recoverable input cost on ≥3 of
5 workloads is an exploratory *technical* threshold — evidence that the detection
mechanism finds something real and sized worth pursuing. It is not evidence that anyone
would pay for it: willingness to pay is a separate, harder question this number cannot
answer on its own. Before concluding "go" off this gate, also look at net dollar value per
workload (not just the percentage — 25% of a small bill is a small number) and get an
explicit answer, not an inferred one, to "would you pay for this" from each workload
owner. Treat the gate as one necessary input to a go/no-go decision, not the decision
itself.
