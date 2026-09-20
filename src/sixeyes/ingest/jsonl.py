"""The zero-instrumentation ingest path: one JSON object per line, no tracing SDK required.

This is deliberately the first adapter. A team with no OTel GenAI instrumentation and no
willingness to integrate anything can still export their logs to this shape by hand, which
is the lowest-friction way to get a first real workload in front of the analyzer -- exactly
the "email me a log file" front door the build plan calls out as closing more deals than a
polished SDK integration.

Documented line schema (fields beyond these are ignored, not rejected):

    {
      "request_id": "req_1",
      "timestamp": 1758000000.0,
      "model": "claude-opus-5",
      "tool_choice": "auto",
      "system": "You are a helpful assistant.",
      "tools": [{"name": "search", "description": "...", "schema": {...}}],
      "messages": [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": null, "tool_calls": [
          {"id": "call_1", "name": "search", "arguments": "{\"query\": \"...\"}"}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "..."}
      ],
      "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 0}
    }

`tools[].schema` may be any JSON-serializable object and is canonicalized on import, so
fingerprinting is insensitive to incidental key-order differences between requests -- a
tool definition is developer-authored and static per request, so nothing real is lost.

A tool call's `arguments` is different: it must be a JSON **string**, exactly as the model
produced it (matching the real wire shape -- OpenAI's own SDK carries
`tool_calls[].function.arguments` as a string, not a parsed object, and that string is not
even guaranteed to be valid JSON). It is stored verbatim, never parsed or re-serialized --
canonicalizing it would silently reformat model output that is itself part of the message
history. A line whose `arguments` is not a string is rejected (`JsonlFormatError`), not
silently coerced. A message's `content` is optional (defaults to empty, not the literal
string "null") -- a real tool-calling assistant turn often carries no text at all, only
`tool_calls`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from sixeyes.core.ids import hash_bytes
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.graph.run_scoped import RunScoped
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolCall, RawToolDef, RawTrace
from sixeyes.ingest.tool_choice import parse_tool_choice


class JsonlFormatError(ValueError):
    """A line did not match the documented schema. Carries the line number so a customer
    pointed at a bad export doesn't have to guess which of possibly thousands of lines is
    wrong."""

    def __init__(self, line_no: int, reason: str) -> None:
        self.line_no = line_no
        super().__init__(f"line {line_no}: {reason}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace differences."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _require(obj: dict[str, Any], field: str, line_no: int) -> Any:
    if field not in obj:
        raise JsonlFormatError(line_no, f"missing required field {field!r}")
    return obj[field]


def _require_float(obj: dict[str, Any], field: str, line_no: int) -> float:
    """Like `_require`, but for a field that must convert to float.

    Deliberately does not include the raw value in the raised error: a malformed field
    can itself contain customer content (a secret pasted into the wrong field, a stray
    string where a number was expected), and `float(x)`'s own ValueError embeds `x`
    verbatim in its message -- which would otherwise flow straight into the run manifest
    (see graph/executor.py's redaction, which only helps if the exception it wraps didn't
    already leak the value in the first place)."""
    value = _require(obj, field, line_no)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise JsonlFormatError(
            line_no, f"field {field!r} must be a number (got type {type(value).__name__})"
        ) from None


def _parse_tool_call(raw: dict[str, Any], line_no: int) -> RawToolCall:
    arguments = raw.get("arguments", "")
    # Explicit rejection, not silent coercion: str(arguments) on a dict would produce
    # Python's repr-ish formatting (single-quoted keys, no real JSON guarantee), which is
    # neither the original model output nor valid JSON -- a corruption that would be worse
    # than just refusing the line. See arguments_raw's docstring for why this must be a
    # string already, not an object to canonicalize.
    if not isinstance(arguments, str):
        raise JsonlFormatError(
            line_no, f"tool call 'arguments' must be a JSON string (got type {type(arguments).__name__})"
        )
    return RawToolCall(
        id=str(_require(raw, "id", line_no)),
        name=str(_require(raw, "name", line_no)),
        arguments_raw=arguments,
    )


def _parse_message(raw: dict[str, Any], line_no: int) -> RawMessage:
    role = _require(raw, "role", line_no)
    if role not in ("system", "user", "assistant", "tool"):
        raise JsonlFormatError(line_no, f"unknown message role {role!r}")
    # content is optional, not merely nullable: a real OpenAI-style assistant message
    # making only tool calls sends content: null with no text at all. str(None) would
    # silently produce the literal 4-character token "None" in every such message's
    # fingerprint -- exactly the kind of corruption a faithful mapping must not do.
    content = raw.get("content")
    return RawMessage(
        role=role,
        content="" if content is None else str(content),
        tool_call_id=raw.get("tool_call_id"),
        tool_calls=tuple(_parse_tool_call(c, line_no) for c in raw.get("tool_calls", [])),
    )


def _parse_tool(raw: dict[str, Any], line_no: int) -> RawToolDef:
    return RawToolDef(
        name=str(_require(raw, "name", line_no)),
        description=str(raw.get("description", "")),
        schema_json=canonical_json(raw.get("schema", {})),
    )


def parse_line(line: str, line_no: int) -> RawRequest:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JsonlFormatError(line_no, f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise JsonlFormatError(line_no, "each line must be a JSON object")

    usage = obj.get("usage", {}) or {}
    try:
        tool_choice = parse_tool_choice(obj["tool_choice"]) if "tool_choice" in obj else None
    except ValueError:
        raise JsonlFormatError(line_no, "unsupported tool_choice shape") from None
    return RawRequest(
        request_id=str(_require(obj, "request_id", line_no)),
        timestamp=_require_float(obj, "timestamp", line_no),
        model=str(_require(obj, "model", line_no)),
        system=obj.get("system"),
        tools=tuple(_parse_tool(t, line_no) for t in obj.get("tools", [])),
        messages=tuple(_parse_message(m, line_no) for m in _require(obj, "messages", line_no)),
        usage_input_tokens=usage.get("input_tokens"),
        usage_output_tokens=usage.get("output_tokens"),
        usage_cache_read_tokens=usage.get("cache_read_input_tokens"),
        tool_choice=tool_choice,
    )


def parse_jsonl(text: str, workload_id: str) -> RawTrace:
    """Parse JSONL content (already read into memory) into a RawTrace, in line order."""
    # A UTF-8 byte-order mark (PowerShell's Out-File, Excel, several Windows exporters) is not
    # whitespace, so strip() left it in front of line 1 and json.loads rejected an otherwise
    # valid export with "Unexpected UTF-8 BOM".
    text = text.removeprefix("﻿")
    requests = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        requests.append(parse_line(stripped, line_no))
    return RawTrace(workload_id=workload_id, requests=tuple(requests))


class JsonlSource(Node):
    """Reads a JSONL trace export from disk.

    Config: `path` (str | Path), `workload_id` (str, defaults to the file stem).

    `config_key()` and `execute()` share exactly one read of the file per run, via
    `RunScoped` (graph/run_scoped.py). A 2026-09-16 follow-up review demonstrated why they
    must: with two independent reads, a file edited between them let the cache-key digest
    (from read #1) and the actually-parsed content (from read #2) disagree -- a downstream
    node's cache key ended up bound to content it never actually processed. A later
    review found the first fix for that (an ad hoc `self._snapshot` flag, force-refreshed
    only in `config_key`) had its own gap: if the forced refresh's read raised
    `FileNotFoundError` (the file was deleted since a prior successful run), the old
    attribute was never reassigned, so `execute()` silently reused the previous run's
    bytes instead of the run correctly failing on a missing file. `RunScoped.resolve(...,
    force=True)` invalidates before recomputing, not after, which closes exactly that gap.
    """

    kind = NodeKind.SOURCE
    version = "4"  # preserve the optional tool_choice control
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    def __init__(self, node_id: str, **config: Any) -> None:
        super().__init__(node_id, **config)
        self._snapshot: RunScoped[tuple[Any, bytes]] = RunScoped()

    def _read_file(self) -> tuple[Any, bytes]:
        path = Path(self.config["path"])
        data = path.read_bytes()
        return (hash_bytes(data), data)

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        path = Path(self.config["path"])
        workload_id = self.config.get("workload_id", path.stem)
        _, data = self._snapshot.resolve(self._read_file)
        text = data.decode("utf-8")
        trace = parse_jsonl(text, workload_id=str(workload_id))
        ctx.log("parsed %d requests from %s", len(trace.requests), path.name)
        return trace

    def config_key(self) -> Any:
        # Keyed by a hash of the file's actual bytes, not mtime: mtime is only a proxy for
        # "did the content change," and a proxy can be wrong -- a preserved or coarse-
        # grained mtime after an edit previously left every downstream node (Fingerprint,
        # Divergence) serving stale, structurally-inconsistent output against a source
        # that had, in fact, changed. A whole-file SHA-256 is safe to fold into a cache key
        # (a directory-lookup string) even though it's derived from content: unlike the
        # per-token chain in fingerprint.fingerprint, there is no adjacent "previous
        # digest" exposed here for an attacker to mount the same word-by-word dictionary
        # test against -- it is one opaque digest of the whole file, not a chain.
        path = Path(self.config["path"])
        try:
            digest, _ = self._snapshot.resolve(self._read_file, force=True)
        except FileNotFoundError:
            digest = None
        return {"path": str(path), "workload_id": self.config.get("workload_id"), "content_digest": digest}
