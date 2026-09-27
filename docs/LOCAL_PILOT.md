# Zero-spend public-source pilot

The founder authorized a read-only task against their public AgentFuse repository:
locate and describe `CircuitBreakerMonitor` using `search_files` and `read_file`.
**Budget: $0 and at most 10 total inference attempts in the session.** The final
zero-spend instruction overrides the earlier $1 allowance. No paid provider, API key,
free-trial billing account or remote fallback is used.

## Pinned dependencies

- AgentFuse commit `9aa96de5d0a9e5a4a5ad65cc42f8b1397fe8813d` (requirements-pilot.txt).
- [Official Qwen3-1.7B GGUF](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF), revision
  `90862c4b9d2787eaed51d12237eafdfe7c5f6077`, file `Qwen3-1.7B-Q8_0.gguf` (~1.8 GB).
  SHA-256: `061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a`.
- [llama.cpp b10964 Windows CPU x64 release](https://github.com/ggml-org/llama.cpp/releases/tag/b10964),
  archive `llama-b10964-bin-win-cpu-x64.zip` (~18 MB).
  Archive SHA-256: `917f39c076402c421224824607397af20f53625a60defc20e8dd22446bf4c5d7`.
  Extract the complete archive, keeping its DLLs beside the executable. Executable
  SHA-256: `aa2e1f5c67be55f11be26ae58d643a545ca07c6de498f6f870330ac2f4dfec73`.

Download from those primary sources with certificate verification enabled, verify the
archive/model hashes, and keep the extracted runtime directory trusted. The launcher
checks the executable/model hashes again before execution. This initial launcher is
deliberately Windows-specific; it is not a universal model installation tool.

## Run

Install development dependencies with `python -m pip install -r requirements-pilot.txt`.
Use a trusted local clone of the public AgentFuse repository containing the pinned commit.
From the SixEyes repository root:

```powershell
python -m pilots.agentfuse.local_run `
  --repo C:\path\to\agentfuse `
  --server-exe C:\path\to\runtime\llama-server.exe `
  --model-file C:\path\to\Qwen3-1.7B-Q8_0.gguf `
  --ledger C:\path\to\this-session\calls.json
```

The runner starts a hidden, offline llama.cpp process bound only to 127.0.0.1, with
8192 context tokens and at most 512 generated tokens per call (temperature 0.2, seed 42).
These are runtime generation settings, not SixEyes's structural unit counts. It disables server logs
and the web UI, then stops its own process on exit. A busy port is rejected. No raw
prompt or completion trace is written; stdout contains only the selected report.

The request client sends exactly the validated adapter kwargs. Generation defaults
are set on the local server, not silently inserted into or removed from captured
requests. Failed HTTP attempts consume the budget; no automatic retries occur. Health
checks are not inference calls. Reuse the **same ledger** on any deliberate repeat or
recovery attempt; deleting it or choosing another path would start another session and
does not authorize additional calls under this experiment.

The router snapshots regular Python Git blobs under `agentfuse/` at the fixed public
revision. It does not read current working files, local edits, symlinks, untracked files,
`.env`, or arbitrary paths. Git replacement objects are disabled. Searches are literal
and limited to 20 results; reads are limited to 100 lines and 6000 characters.

## Evidence boundary

### Optional Windows Vulkan GPU backend

Use the same b10964 release's `llama-b10964-bin-win-vulkan-x64.zip` (31,674,542 bytes).
Archive SHA-256: `1ee3ad952f4ba71f438bd6d7bebef19e1c7af04adcaa35d08b4ddabb27d4c642`.
Verify before extracting the complete archive into a separate trusted directory. The
server executable hash matches the CPU build; `ggml-vulkan.dll` must additionally match
`a2caa6ce515f60a2f60e758ad78b1e15d028512cc7e27d278f41e468b94100fe`.
Run that executable with `--list-devices`, then point `--server-exe` at it and append
`--device Vulkan1` (replace with the actual desired device identifier). Ordering can vary.
The launcher requests 99 GPU layers; it does not certify that every layer fits in VRAM.
Without `--device`, CPU execution is explicitly selected. No driver installation is needed
on the tested machine. Reuse the original ledger, including across CPU/GPU changes.

The CLI now returns a nonzero status for max-turns/incomplete outcomes, even when enough
requests exist for a structural report. Completion still does not establish answer quality.

This runs a real open-weight model and the actual AgentFuse adapter/monitor against
public source. It is neither a scripted-response test nor a paying customer's workload.
The model generates requests outside SixEyes; fingerprinting/comparison uses no model.

The report includes attempted/successful calls, tool counts, completion state and
structural comparisons. It omits raw source, prompts and model answers. An adapter's
`complete` status or a target-name mention does **not** establish answer quality. A
`none` divergence does **not** establish a provider cache hit or saved money. An empty
or single-request trace provides no consecutive-pair evidence and is reported as such.

## Execution record

Completed on 2026-09-18 after model/archive checksum verification. See the
[selected machine-readable report](results/local-pilot-2026-09-18.json).

- 3 inference attempts, all successfully captured; 7 of the session's 10 calls unused.
- 1 search and 1 read, zero rejected tools; adapter completed with finish reason `stop`.
- 3 requests yielded 2 consecutive comparisons, both `none` (zero changed pairs).
- $0 provider charge. CPU time, electricity and download bandwidth were still consumed.
- Answer quality was not independently verified; no raw answer or request was retained.

An initial launcher attempt failed before inference because the sandbox-owned Git clone
was not trusted by the host account. A process-scoped safe.directory exception for that
specific clone resolved it; it consumed no inference call. The same counts-only ledger
was used throughout. The owned server stopped after execution.

This is evidence that real local tool-use requests survive the capture/fingerprint/report
path. It found no actionable divergence and provides no savings, provider-cache or
willingness-to-pay evidence. Do not count it as passing the business kill gate.

### GPU follow-up — 2026-09-27

[Selected GPU report](results/gpu-pilot-2026-09-27.json). NVIDIA RTX 3050 Laptop GPU,
4096 MiB, driver 555.97; selected Vulkan1 after device enumeration. During execution,
nvidia-smi listed the owned llama-server process and 2834 MiB total GPU memory in use
(including other applications). No per-process memory or speedup claim is made.

The run used the remaining 7 calls: 1 search, 6 reads, zero rejected tools, 7 captures
and 6 comparisons all `none`. It ended at `max_turns`, still requesting a tool, with no
final answer. It is an incomplete task, not a successful task-quality result. The initial
runner exit code was zero because it only checked transport/comparison availability;
that condition has now been corrected to require adapter completion as well.

Provider charge remained $0. The same ledger now records 10/10 attempts; no more calls
are permitted under this session. The owned server exited and no raw captures or answers
were retained. Different behavior across the CPU and GPU runs does not establish its
cause; a seed alone is not evidence of identical outputs across backends. Stable structural
prefixes also do not establish useful progress, redundant-tool-call waste, or savings.

### Next experiment: repeated requests and task termination

The owner explicitly approved one additional 10-call local GPU session at $0 on the same
public AgentFuse source. The original exhausted ledger is unchanged; the new ledger is
`session-2026-09-27-observations/calls.json` in the local model workspace.

Question: does the tool loop repeat identical read requests, reach a final text response,
or stop at a limit while its structural comparisons remain unchanged? The previous run's
six reads cannot answer this: no read identities were retained, and counts alone do not
establish repetition. Do not retroactively infer them.

The router counts repeated accepted (path, start_line, max_lines) triples after applying
defaults. One initial read followed by two identical reads yields two repeats. Changed
ranges, invalid requests, overlapping-but-different ranges and searches do not count.
Only the count is exported, never paths, arguments, tool output or read identities.
This is pilot instrumentation outside the detector graph, not a Phase 3 waste detector.
The workload always executes the requested read; nothing is optimized or suppressed.

`run_outcome` separately describes failure, escalation, call limit, output limit, absence
of final text, or `finished_with_text`. The last label is not a quality verdict. The
launcher requires that label and enough captured calls for comparison before exiting
successfully. A repetition observation alone cannot establish whether a repeated read
was unnecessary or how much input cost it caused. Useful next decisions require the
task outcome and the workload owner's assessment, not simply a high repeat count.

**Observed 2026-09-27:** [selected report](results/observed-pilot-2026-09-27.json).
All 10 attempts were captured: one search, nine reads, zero repeated read requests and
zero rejected tools. Nine comparisons were `none`. The adapter ended at `max_turns`
with finish reason `tool_calls`; `run_outcome` was `incomplete_call_limit` and the CLI
returned 1. No final answer was produced. $0 provider charge; this new ledger is now
exhausted too. No raw requests, results or completions were retained.

This run does not support identical read requests as the failure explanation. Different
ranges may still overlap, and useful progress is not determined by request counts.
Neither cause of failure nor recoverable spend is established. See the
[experiment decision record](EXPERIMENT_DECISIONS.md) before adding another detector or
running more calls.
