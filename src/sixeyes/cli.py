"""Local structural analysis; no provider calls, persistent cache or raw export."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import NoReturn

from sixeyes.fingerprint.divergence import Divergence
from sixeyes.fingerprint.fingerprint import Fingerprint
from sixeyes.graph import Executor, Graph
from sixeyes.graph.cache import NullCache
from sixeyes.ingest.jsonl import JsonlFormatError
from sixeyes.ingest.memory import InMemoryTraceSource
from sixeyes.ingest.strict_jsonl import parse_strict_jsonl
from sixeyes.ingest.types import RawTrace
from sixeyes.report import StructuralReport

MAX_INPUT_BYTES = 1024 * 1024


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "Invalid command arguments; run python -m sixeyes --help.\n")


async def analyze_trace(trace: RawTrace) -> StructuralReport:
    graph = Graph("local-analysis")
    graph.add(InMemoryTraceSource("source", trace=trace))
    graph.add(Fingerprint("fingerprint", key=secrets.token_bytes(32)), trace="source")
    graph.add(Divergence("divergence", workload_id="local"), fingerprints="fingerprint")
    result = await Executor(cache=NullCache()).run(graph, targets=["divergence"])
    return StructuralReport(len(trace.requests), result["divergence"])


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description="Analyze one authorized session in SixEyes normalized JSONL format, locally.")
    parser.add_argument("command", choices=["analyze"])
    parser.add_argument("input", type=Path, help="Existing UTF-8 JSONL; one session, request order preserved")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--fail-on-change", action="store_true", help="Exit 1 if any structural change is observed")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            print("Input exceeds the 1 MiB limit.", file=sys.stderr)
            return 2
        trace = parse_strict_jsonl(data.decode("utf-8"))
        report = asyncio.run(analyze_trace(trace))
    except JsonlFormatError as exc:
        print(f"Invalid normalized JSONL at line {exc.line_no}; see docs/LOCAL_ANALYSIS.md.", file=sys.stderr)
        return 2
    except Exception:
        # Input paths, parser payloads and execution exceptions may carry raw content.
        print("Local analysis failed; check file access, UTF-8 encoding and the documented schema.", file=sys.stderr)
        return 2
    payload = report.to_dict()
    print(json.dumps(payload, indent=2) if args.format == "json" else report.render_text())
    if report.request_count < 2:
        return 3
    return 1 if args.fail_on_change and payload["changed_pair_count"] else 0
