"""Run with python -m pilots.agentfuse; synthetic offline mode only."""

import argparse
import asyncio
import json
import sys

from pilots.agentfuse.demo import capture_demo
from pilots.agentfuse.report import analyze_captured_calls


def main() -> int:
    parser = argparse.ArgumentParser(description="SixEyes synthetic offline AgentFuse demo (no paid calls)")
    parser.add_argument("--scenario", choices=("stable", "system-drift", "restart"), default="stable")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        captured = capture_demo(args.scenario)
        report = asyncio.run(analyze_captured_calls(captured))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps({"mode": "synthetic_offline", "scenario": args.scenario, **report.to_dict()}, indent=2))
    else:
        print(f"SYNTHETIC OFFLINE DEMO: {args.scenario}\n{report.render_text()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
