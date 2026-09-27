# Analyze one workload session locally

This command needs Python 3.11+ and the SixEyes package. It does not need AgentFuse,
a model, a GPU, a provider account or a network connection. It makes no model calls.
It answers which supported structural segment first changed between consecutive
requests. It does not prove a cache miss, unnecessary work, task quality or savings.

From an installed checkout:

```powershell
python -m pip install -e .
python -m sixeyes analyze examples/structural-change.jsonl
python -m sixeyes analyze C:\path\to\authorized-session.jsonl --format json
```

The included example is synthetic. Its expected result is one `system_changed`
comparison. For an optional regression check, add `--fail-on-change`. Intentional
changes, such as recovery restarts, can also fail that check; review the context.

## Input contract

Supply an existing, owner-authorized export in **SixEyes normalized JSONL**, UTF-8,
with optional BOM and blank lines. One object per line, in request order, for **one
logical session only**. The command cannot infer session boundaries or sort your trace.
Split unrelated sessions before analysis. Limits are 1 MiB and 1000 requests per file;
this is a bounded first-pilot interface, not a large-log ingestion service.

It is not a direct reader for arbitrary provider logs or OpenAI messages arrays.
Unknown fields, duplicate JSON keys, non-finite numbers, non-text message content and
unsupported shapes fail explicitly. Normalize only fields you understand; do not drop
unsupported controls or multimodal data to make an error disappear. The older Python
`parse_jsonl` API remains permissive for compatibility; this command uses stricter intake.

```json
{"request_id":"r1","timestamp":1,"model":"example-model","system":"Static instructions","tool_choice":"auto","tools":[{"name":"search","description":"Search public text","schema":{"type":"object","properties":{"query":{"type":"string"}}}}],"messages":[{"role":"user","content":"Find a definition"}],"usage":{"input_tokens":42,"output_tokens":8,"cache_read_input_tokens":0}}
```

- Required: nonempty string `request_id` and `model`, finite numeric `timestamp`,
  and a `messages` array. Timestamp does not affect ordering.
- Optional `system`: string or null. Optional `tools`: array or null; each definition
  has a nonempty `name`, optional string `description`, and optional object `schema`.
- Message roles: system, user, assistant, tool. Content is a string, null or omitted.
  Tool-result messages require a nonempty string `tool_call_id`; other roles cannot bind one.
- Assistant `tool_calls`: array or null of objects with nonempty string `id`, `name`,
  and string `arguments`. Preserve that argument string exactly, including whitespace
  or malformed model JSON; do not parse/reformat it. Other roles cannot carry calls.
- Optional `tool_choice`: `auto`, `none`, `required`, or
  `{"type":"function","function":{"name":"search"}}`. Omission differs from explicit auto.
- Optional `usage`: object or null with nonnegative integer/null `input_tokens`,
  `output_tokens`, `cache_read_input_tokens`. These counters are not used to infer savings.

## Output and exits

Text is the default; `--format json` emits schema version 1 with request and comparison
counts, request ordinals, fixed change kinds, structural unit offsets and suggested
checks. It omits request IDs, model names, paths, timestamps, raw content, hashes and keys.
Offsets are neither provider-token positions nor exact nested-field locations.

| Exit | Meaning |
| --- | --- |
| 0 | Analysis completed with at least two requests; changes may exist unless fail-on-change was selected |
| 1 | Structural change observed with `--fail-on-change` |
| 2 | Invalid arguments/input, size/request limit, or analysis failure; no partial report |
| 3 | Fewer than two requests; report has insufficient comparison evidence |

The command reads the file but does not modify it, create a raw copy, persist a key,
write a cache, or export a graph manifest. Raw text exists transiently in memory; there
is no secure-erasure or OS-sandbox guarantee. Reports still reveal structural metadata.
Errors expose fixed guidance and, for invalid records, a line number rather than raw
parser messages. Shell redirection is under the caller's control: never redirect output
back to the input file, since the shell can truncate it before the command starts.

## How to evaluate usefulness

Before running a real workload, write down the suspected issue and what the owner's
current tools show. Inspect the reported boundary locally and decide whether the change
was intentional. Ask whether SixEyes added a new, actionable explanation. A proposed fix
needs its own quality check and observed provider usage; an unchanged report is not a
cache-hit or successful-task claim. Do not upload raw traces to the public repository.
See [the experiment decision record](EXPERIMENT_DECISIONS.md).
