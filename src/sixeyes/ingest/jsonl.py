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
      "system": "You are a helpful assistant.",
      "tools": [{"name": "search", "description": "...", "schema": {...}}],
      "messages": [{"role": "user", "content": "..."}],
      "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 0}
    }

`tools[].schema` may be any JSON-serializable object; it is canonicalized on import so
fingerprinting is insensitive to incidental key-order differences between requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from sixeyes.core.ids import hash_bytes
from sixeyes.graph.context import RunContext
from sixeyes.graph.node import Node, NodeKind
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolDef, RawTrace


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


def _parse_message(raw: dict[str, Any], line_no: int) -> RawMessage:
    role = _require(raw, "role", line_no)
    if role not in ("system", "user", "assistant", "tool"):
        raise JsonlFormatError(line_no, f"unknown message role {role!r}")
    return RawMessage(
        role=role,
        content=str(_require(raw, "content", line_no)),
        tool_call_id=raw.get("tool_call_id"),
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
    )


def parse_jsonl(text: str, workload_id: str) -> RawTrace:
    """Parse JSONL content (already read into memory) into a RawTrace, in line order."""
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

    `config_key()` and `execute()` share exactly one read of the file, taken whichever of
    the two runs first (in the normal `Executor.run()` flow, always `config_key()`, since
    `Graph.cache_keys()` computes every node's key before any node executes) and cached on
    the instance. A 2026-09-16 follow-up review demonstrated why they must: with two
    independent reads, a file edited between them let the cache-key digest (from read #1)
    and the actually-parsed content (from read #2) disagree -- a downstream node's cache
    key ended up bound to content it never actually processed, so a later run whose first
    read happened to match read #1 again served that stale, mismatched result back. One
    shared snapshot makes the key and the parsed content provably describe the same bytes.
    """

    kind = NodeKind.SOURCE
    version = "2"
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    def __init__(self, node_id: str, **config: Any) -> None:
        super().__init__(node_id, **config)
        self._snapshot: tuple[Any, bytes] | None = None  # (digest, raw file bytes)

    def _read_snapshot(self, *, force: bool = False) -> tuple[Any, bytes]:
        """`force=True` (used only by `config_key`) always re-reads: a node instance can
        be reused across multiple separate `Executor.run()` calls sharing one Graph (a
        normal, supported pattern -- several tests do exactly this), and each such run
        needs its own fresh read, not the previous run's cached one. `config_key()` is
        guaranteed to run exactly once, near the very start of every `Executor.run()`
        (via `Graph.cache_keys()`, which completes before any node's `execute()` begins),
        so calling it with `force=True` there and leaving `execute()`'s default reuse-if-set
        behaviour gives each run exactly one fresh read, shared between the two."""
        if force or self._snapshot is None:
            path = Path(self.config["path"])
            data = path.read_bytes()
            self._snapshot = (hash_bytes(data), data)
        return self._snapshot

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        path = Path(self.config["path"])
        workload_id = self.config.get("workload_id", path.stem)
        _, data = self._read_snapshot()
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
            digest, _ = self._read_snapshot(force=True)
        except FileNotFoundError:
            digest = None
        return {"path": str(path), "workload_id": self.config.get("workload_id"), "content_digest": digest}
