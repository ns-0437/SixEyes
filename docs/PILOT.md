# Offline AgentFuse pilot

The immediate goal is a report a workload owner can inspect. This prototype compares
request structure. It does not demonstrate cache hits, saved money, redundant tool calls
or customer demand. It remains part of Phase 2 pilot preparation; the product CLI and
economic reporting in Phase 6 are not complete.

## Run the demo

From the repository root, using Python 3.11+ and Git:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS instead: source .venv/bin/activate
python -m pip install -r requirements-pilot.txt
python -m pilots.agentfuse --scenario stable
python -m pilots.agentfuse --scenario system-drift
python -m pilots.agentfuse --scenario restart --format json
```

Installation downloads dependencies. Running these scenarios makes no provider calls:
the client returns fixed responses and the router returns synthetic in-memory data.
The entry point has no live mode, credentials option or raw trace-file output. It writes
its selected report to stdout; you can redirect that report to a file for review.

The real `agentfuse.adapters.openai_sdk.guarded_tool_loop` drives every demo. AgentFuse
is pinned to `9aa96de5d0a9e5a4a5ad65cc42f8b1397fe8813d` as a development dependency,
not bundled into SixEyes. Alternatively install `.[dev]` and set `AGENTFUSE_PATH` to a
trusted checkout root. Explicitly missing/broken checkouts fail rather than silently
skipping integration tests. The simplified `run_scripted_loop` is not used by this demo.

| Scenario | Expected evidence |
| --- | --- |
| `stable` | Two requests; one comparison reporting `none` for appended tool history. |
| `system-drift` | Two requests; an intentionally injected system change is localized. |
| `restart` | Three requests; ordinary growth followed by `messages_changed` when the real adapter restarts. |

An early restart can look like normal growth if discarded turns were never sent in a
previous request. The restart fixture waits until a tool turn has been sent so there is
an observable history change. Intentional restarts are not automatically waste.

## Supported data and report boundary

Supported: one leading system message; text-only user/assistant/tool messages; function
tools; raw string arguments; result bindings; `tool_choice` modes `auto`, `none`,
`required`, or a named function object. The choice subset follows the
[official Chat Completions schema](https://developers.openai.com/api/reference/resources/chat#chat-completions-tool-choice-option)
(checked 2026-09-17). Other modes/options, function `strict`, message `name`, and
multimodal content are explicitly rejected in this pilot. This is not a general SDK importer.

Tool choice is HMAC-keyed separately from ordered tools/system/messages segments. A
changed choice reports `tool_choice_changed` before segment differences; no token
position or cache impact is inferred. Omission is distinct from explicit default modes.
The JSONL importer accepts the same optional field, but otherwise retains its existing,
more permissive schema; do not describe all JSONL fields as strictly validated.

The analyzer uses the real graph executor, an ephemeral random HMAC key and NullCache.
Raw request snapshots exist in memory. The caller retains those objects; this is not a
secure-erasure guarantee. The selected report emits request ordinals, counts, fixed
change kinds, structural unit offsets and static next checks. It omits raw text, function
names, model names, arbitrary workload labels, timestamps and graph manifests.

Offsets are not provider tokens. `none` is not proof of a cache hit. Model/control
changes have no segment offset in the exported report. Only the first detected change
per pair is reported; this does not enumerate every field difference.

## Verification

```bash
python -m pytest -q
python -m mypy
```

The pinned pilot dependency enables the real-adapter cases; without it those cases skip.
CI explicitly requires the adapter to import before tests. Tests block outbound socket
connections during demo execution, allowing the stdlib socket pair used by Windows
asyncio. This is a test assertion, not a production network sandbox.

## First real-workload experiment

Before executing anything paid, record the owner-authorized non-sensitive task/data,
provider/model, maximum billable calls (including retries), and spending controls. The
current demo does not implement a paid-run budget guard and must not be presented as one.
The live capture hook and enforcement still need implementation and offline verification
against that selected runtime. Do not substitute a real client into the synthetic demo.

For one session, keep capture and fingerprinting on the owner's machine, examine any
structural changes there, and record whether they were intentional and whether existing
tools already explained them. Ask which observation could justify a concrete change.
Evaluate an applied change using observed usage and a task-quality check before discussing
savings; a structural report alone supports neither a dollar figure nor quality preservation.

Ask the owner what they would pay for the next useful analysis, and record the answer
separately from technical results. No useful new observation is a legitimate negative
result. It should narrow or stop the experiment rather than trigger a larger platform build.
