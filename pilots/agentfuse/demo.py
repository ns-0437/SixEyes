"""Synthetic scenarios driven through the actual optional AgentFuse adapter."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from pilots.agentfuse.fake_client import (
    CapturedCall, FakeChatCompletion, FakeChoice, FakeFunction, FakeMessage,
    FakeToolCall, ScriptedClient,
)


def load_agentfuse() -> Any:
    checkout = os.environ.get("AGENTFUSE_PATH")
    if checkout:
        root = Path(checkout).resolve()
        if not (root / "agentfuse" / "adapters" / "openai_sdk.py").is_file():
            raise RuntimeError("AGENTFUSE_PATH must point to an AgentFuse checkout root")
        sys.path.insert(0, str(root))
    try:
        module = importlib.import_module("agentfuse.adapters.openai_sdk")
    except ImportError:
        raise RuntimeError(
            "AgentFuse is required for the offline demo. Install with pip install -r requirements-pilot.txt "
            "or set AGENTFUSE_PATH to a checkout."
        ) from None
    if checkout and (module.__file__ is None or
                     Path(module.__file__).resolve() != root / "agentfuse" / "adapters" / "openai_sdk.py"):
        raise RuntimeError("An AgentFuse module from another checkout is already loaded")
    return module


def capture_demo(scenario: str) -> list[CapturedCall]:
    if scenario not in ("stable", "system-drift", "restart"):
        raise ValueError("unsupported synthetic scenario")
    adapter = load_agentfuse()
    events = importlib.import_module("agentfuse.events")
    monitor = importlib.import_module("agentfuse.monitor")

    class QuietMonitor:
        # Only drives the restart branch. No AgentFuse tracer, memory store or model.
        restarted = False
        results_seen = 0

        def observe(self, event: Any) -> Any:
            if event.type is events.EventType.TOOL_RESULT:
                self.results_seen += 1
            if scenario == "restart" and not self.restarted and self.results_seen == 2:
                self.restarted = True
                return monitor.Directive(kind=monitor.DirectiveKind.INJECT, steering_text="Try a fresh approach.")
            return monitor.Directive(kind=monitor.DirectiveKind.CONTINUE)

        def finish(self, status: str = "complete") -> dict[str, str]:
            return {"status": status}

    tool_response = FakeChatCompletion((FakeChoice(FakeMessage(tool_calls=(
        FakeToolCall("synthetic_call", FakeFunction("search", '{"query": "demo"}')),
    ))),))
    text_response = FakeChatCompletion((FakeChoice(FakeMessage(content="Synthetic task complete.")),))
    second_tool_response = FakeChatCompletion((FakeChoice(FakeMessage(tool_calls=(
        FakeToolCall("synthetic_call_2", FakeFunction("search", '{"query": "another demo"}')),
    ))),))
    script = [tool_response, second_tool_response, text_response] if scenario == "restart" else [tool_response, text_response]
    client = ScriptedClient(script)
    adapter.guarded_tool_loop(
        client, model="synthetic-model", system_prompt="Synthetic system prompt.",
        user_input="Search the in-memory demo data.",
        tools=[{"type": "function", "function": {
            "name": "search", "parameters": {"type": "object"},
        }}],
        tool_router=lambda name, args: "Synthetic result.",
        max_turns=len(script), monitor=QuietMonitor(),
    )
    if scenario == "system-drift":
        # Deliberate fixture injection, not something attributed to AgentFuse.
        client.captured[1].messages[0]["content"] += " Injected changing timestamp."
    return client.captured
