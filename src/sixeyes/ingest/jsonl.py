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
        timestamp=float(_require(obj, "timestamp", line_no)),
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
    """

    kind = NodeKind.SOURCE
    version = "1"
    inputs: ClassVar[dict[str, type]] = {}
    output = RawTrace
    content_bearing = True

    async def execute(self, ctx: RunContext, **_: object) -> RawTrace:
        path = Path(self.config["path"])
        workload_id = self.config.get("workload_id", path.stem)
        text = path.read_text(encoding="utf-8")
        trace = parse_jsonl(text, workload_id=str(workload_id))
        ctx.log("parsed %d requests from %s", len(trace.requests), path.name)
        return trace

    def config_key(self) -> Any:
        # SOURCE nodes are cache-keyed by their *declared* fingerprint (rule: cacheable by
        # declared fingerprint), not by re-reading the file -- mtime stands in for content
        # here so an unchanged file is a cache hit and an edited one isn't, without hashing
        # the (content-bearing) file body itself into a cache key.
        path = Path(self.config["path"])
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        return {"path": str(path), "workload_id": self.config.get("workload_id"), "mtime": mtime}
