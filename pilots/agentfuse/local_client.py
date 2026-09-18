"""Local llama.cpp transport: literal loopback, no credentials, redirects or retries."""

from __future__ import annotations

import copy
import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

from pilots.agentfuse.bridge import convert_captured_calls
from pilots.agentfuse.capture import CapturedCall, Choice, Completion, FunctionCall, Message, ToolCall, Usage


class LocalPilotError(RuntimeError):
    """Fixed, content-free diagnostic for a failed local pilot operation."""


class CallBudget:
    """Reserve before POST, including failures; reuse this ledger across session runs.

    A concurrent reservation fails closed. The on-disk ledger contains counts only.
    Deleting the ledger starts a different budget, so the runner must reuse its path.
    """

    def __init__(self, path: Path, limit: int = 10) -> None:
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("call limit must be between 1 and 10")
        self.path = path
        self.limit = limit

    def reserve(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            handle = lock.open("x")
        except FileExistsError:
            raise LocalPilotError("Call budget is locked by another operation") from None
        try:
            with handle:
                count = self.used()
                if count >= self.limit:
                    raise LocalPilotError("Session inference-call limit reached")
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                with temporary.open("w", encoding="utf-8") as output:
                    json.dump({"version": 1, "limit": self.limit, "attempted_calls": count + 1}, output)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.path)
                return count + 1
        finally:
            lock.unlink()

    def used(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            count = data["attempted_calls"]
            if (set(data) != {"version", "limit", "attempted_calls"} or data["version"] != 1
                    or data["limit"] != self.limit or type(count) is not int or not 0 <= count <= self.limit):
                raise ValueError
            return count
        except (ValueError, KeyError, TypeError, OSError):
            raise LocalPilotError("Call budget ledger is invalid; refusing to reset it") from None


def _completion(value: Any) -> Completion:
    """Validate the response subset the actual AgentFuse driver reads."""
    try:
        choices = value["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        message = choices[0]["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        calls = []
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list) or len(raw_calls) > 4:
            raise ValueError
        for call in raw_calls:
            fn = call["function"]
            if call["type"] != "function" or not all(
                isinstance(v, str) for v in (call["id"], fn["name"], fn["arguments"])
            ):
                raise ValueError
            calls.append(ToolCall(call["id"], FunctionCall(fn["name"], fn["arguments"])))
        usage = value.get("usage")
        counts = None
        if usage is not None:
            tin, tout = usage["prompt_tokens"], usage["completion_tokens"]
            if type(tin) is not int or type(tout) is not int or tin < 0 or tout < 0:
                raise ValueError
            counts = Usage(tin, tout)
        return Completion((Choice(Message(content, tuple(calls))),), counts)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise LocalPilotError("Local server returned an unsupported completion shape") from None


class _Completions:
    def __init__(self, owner: LocalClient) -> None:
        self.owner = owner

    def create(self, **kwargs: Any) -> Completion:
        return self.owner.create(**kwargs)


class _Chat:
    def __init__(self, owner: LocalClient) -> None:
        self.completions = _Completions(owner)


class LocalClient:
    """Only communicates with a caller-started local llama.cpp server on 127.0.0.1.

    No arbitrary base URL, proxy/environment handling or remote fallback. Server
    generation defaults are configured at launch, so the captured body is exactly
    the supported request the adapter sent. Inference remains outside detection.
    """

    def __init__(self, *, port: int, budget: CallBudget) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid local server port")
        self.port = port
        self.budget = budget
        self.chat = _Chat(self)
        self.captured: list[CapturedCall] = []
        self.last_finish_reason: str | None = None

    def create(self, **kwargs: Any) -> Completion:
        snapshot = copy.deepcopy(kwargs)
        captured_at = time.time()
        extra = {k: v for k, v in snapshot.items() if k not in {"model", "messages", "tools"}}
        def capture(response: Completion) -> CapturedCall:
            return CapturedCall(snapshot.get("model", ""), snapshot.get("messages", []),
                                snapshot.get("tools", []), extra, response, captured_at)
        # Reject unsupported request shapes before spending even a local inference call.
        convert_captured_calls([capture(Completion(()))], "local-pilot")
        try:
            body = json.dumps(snapshot, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise LocalPilotError("Request is not supported JSON") from None
        if len(body) > 48_000:
            raise LocalPilotError("Local pilot request size limit reached")
        self.budget.reserve()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=240)
        try:
            connection.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise LocalPilotError("Local server rejected the inference request")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise LocalPilotError("Local response size limit reached")
            data = json.loads(raw)
            result = _completion(data)
            reason = data["choices"][0].get("finish_reason")
            self.last_finish_reason = reason if reason in ("stop", "tool_calls", "length") else "unknown"
        except (OSError, http.client.HTTPException, ValueError):
            raise LocalPilotError("Local inference failed; attempt counted, no automatic retry") from None
        finally:
            connection.close()
        self.captured.append(capture(result))
        return result
