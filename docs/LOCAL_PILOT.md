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
