# First workload decision record — 2026-09-27

SixEyes's local integration works, but these experiments have not established an
actionable cache problem, recoverable spend, or willingness to pay. AgentFuse is a
founder-owned public test workload, not an independent customer or a required SixEyes
runtime dependency.

| Run | Calls | Searches / reads | Exact repeated reads | Termination | Structural changes |
| --- | ---: | --- | --- | --- | ---: |
| CPU, September 18 | 3 | 1 / 1 | Not instrumented | Final text; quality unverified | 0 of 2 pairs |
| GPU, September 27 | 7 | 1 / 6 | Not instrumented | Call limit, no final answer | 0 of 6 pairs |
| GPU with observations, September 27 | 10 | 1 / 9 | 0 | Call limit, no final answer | 0 of 9 pairs |

The first two runs shared the original 10-call allowance; the third used a separately
approved 10-call allowance. Both are exhausted. Provider spend was $0 throughout.
Reports and runtime/source pins are linked from [LOCAL_PILOT.md](LOCAL_PILOT.md).

## What changed in our understanding

- Stable structural prefixes can coexist with an incomplete task. The existing
  divergence report does not measure quality, progress, or completion.
- The last run did not repeat an identical accepted (path, start_line, max_lines)
  read request. This rules out that exact behavior in that run, not overlapping reads
  or repetition elsewhere. No claim can be made about the earlier uninstrumented runs.
- AgentFuse intentionally rewrites history during recovery. A structural change at
  such a boundary needs its recovery context before anyone recommends preserving it.
- No controlled comparison isolated why CPU and GPU outcomes differed. The same seed
  is not proof of identical execution across backends. Avoid a GPU-caused-failure claim.

## Decision

Keep the local capture/report integration and the bounded observations. Do not build
an automatic repeated-read optimizer, call the observed reads waste, expand runtime
infrastructure, or begin Phase 4 economics based on these runs. There is no owner-useful
spend finding yet. The five-workload kill gate has not been met.

The next product-validation input should be an owner-approved workload with a concrete
suspected cost symptom and an existing diagnostic baseline: for example, an owner
observing inconsistent cache reuse and wanting to identify which request boundary changes.
Record what their existing tools show first, then test whether SixEyes adds a useful
explanation. Any observed change still needs owner confirmation that it is unintended;
any proposed fix needs separate quality and actual-usage checks.

If AgentFuse remains the engineering test, the next narrow hypothesis is whether more
targeted source navigation can finish within a fixed budget. A candidate instruction is
to search separately for the class, `def observe`, and `def finish`, then inspect bounded
ranges around those definitions. That changes the workload instruction, so it needs a
controlled comparison and outcome check; it is not an already-verified remediation.
Do not spend further inference calls without a new bounded authorization.

## Evidence needed before a business go decision

For each independently authorized workload, record: owner and scope privately, suspected
cost issue, their current diagnostic method, SixEyes's added observation, whether it was
new/actionable, the accepted or rejected fix, quality checks, observed usage before/after,
and an explicit willingness-to-pay answer. Keep customer identifiers and raw traces out
of public records. An engineering run with no new observation is an honest negative,
not a customer win.
