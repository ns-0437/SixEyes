"""No real inference in tests; local-call controls and public-source boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pilots.agentfuse.local_client import CallBudget, LocalClient, LocalPilotError
from pilots.agentfuse.repo_tools import PublicRepoTools
from pilots.agentfuse.local_runtime import verify_file


def test_budget_survives_new_clients_and_rejects_eleventh_attempt(tmp_path: Path) -> None:
    ledger = tmp_path / "calls.json"
    for index in range(10):
        assert CallBudget(ledger).reserve() == index + 1
    with pytest.raises(LocalPilotError, match="limit reached"):
        CallBudget(ledger).reserve()
    assert json.loads(ledger.read_text())["attempted_calls"] == 10


@pytest.mark.parametrize("raw", ['{"attempted_calls": -1}', '[]', 'not json'])
def test_budget_corruption_fails_closed(tmp_path: Path, raw: str) -> None:
    ledger = tmp_path / "calls.json"
    ledger.write_text(raw)
    with pytest.raises(LocalPilotError, match="invalid"):
        CallBudget(ledger).reserve()
    assert ledger.read_text() == raw
    assert not ledger.with_suffix(".json.lock").exists()


def test_budget_rejects_overlap_and_limit_changes(tmp_path: Path) -> None:
    ledger = tmp_path / "calls.json"
    ledger.with_suffix(".json.lock").touch()
    with pytest.raises(LocalPilotError, match="locked"):
        CallBudget(ledger).reserve()
    ledger.with_suffix(".json.lock").unlink()
    CallBudget(ledger, 3).reserve()
    with pytest.raises(LocalPilotError, match="invalid"):
        CallBudget(ledger, 10).reserve()


def test_model_checksum_mismatch_refuses_execution(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"wrong model")
    with pytest.raises(LocalPilotError, match="checksum mismatch"):
        verify_file(model, "0" * 64)


def _kwargs() -> dict[str, Any]:
    return {"model": "local", "messages": [{"role": "system", "content": "sys"}], "tools": []}


def test_failed_requests_consume_budget_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    class FailedConnection:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert host == "127.0.0.1" and port == 18765

        def request(self, *args: Any) -> None:
            requests.append(args)
            raise OSError("SYNTHETIC-SECRET")

        def close(self) -> None:
            pass

    monkeypatch.setattr("pilots.agentfuse.local_client.http.client.HTTPConnection", FailedConnection)
    budget = CallBudget(tmp_path / "calls.json", 1)
    client = LocalClient(port=18765, budget=budget)
    with pytest.raises(LocalPilotError) as error:
        client.create(**_kwargs())
    assert "SYNTHETIC-SECRET" not in str(error.value)
    with pytest.raises(LocalPilotError, match="limit reached"):
        client.create(**_kwargs())
    assert len(requests) == 1 and budget.used() == 1 and not client.captured


@pytest.mark.parametrize("status", [200, 302, 500])
def test_wire_snapshot_and_no_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    bodies = []

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            assert method == "POST" and path == "/v1/chat/completions"
            assert set(headers) == {"Content-Type"}
            bodies.append(json.loads(body))

        def getresponse(self) -> Any:
            return type("Response", (), {"status": status, "read": lambda self, limit: json.dumps({
                "choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "search_files", "arguments": '{bad json'}},
                ]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }).encode()})()

        def close(self) -> None:
            pass

    monkeypatch.setattr("pilots.agentfuse.local_client.http.client.HTTPConnection", Connection)
    client = LocalClient(port=18765, budget=CallBudget(tmp_path / "calls.json"))
    kwargs = _kwargs()
    if status != 200:
        with pytest.raises(LocalPilotError):
            client.create(**kwargs)
        assert not client.captured
    else:
        response = client.create(**kwargs)
        assert response.choices[0].message.tool_calls[0].function.arguments == '{bad json'
        assert bodies == [kwargs]
        kwargs["messages"][0]["content"] = "mutated later"
        assert client.captured[0].messages[0]["content"] == "sys"
    assert len(bodies) == client.budget.used() == 1


def test_unsupported_request_rejected_before_local_call(tmp_path: Path) -> None:
    client = LocalClient(port=18765, budget=CallBudget(tmp_path / "calls.json"))
    with pytest.raises(ValueError):
        client.create(**_kwargs(), api_key="SYNTHETIC-SECRET")
    assert client.budget.used() == 0


def test_router_reads_pinned_blobs_not_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    (tmp_path / "agentfuse").mkdir()
    source = tmp_path / "agentfuse" / "monitor.py"
    source.write_text("class CircuitBreakerMonitor:\n    def observe(self): pass\n")
    (tmp_path / ".env").write_text("SYNTHETIC-SECRET")
    git("add", "agentfuse/monitor.py", ".env")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    monkeypatch.setattr("pilots.agentfuse.repo_tools.AGENTFUSE_REVISION", git("rev-parse", "HEAD"))
    source.write_text("SYNTHETIC-LOCAL-EDIT")
    (tmp_path / "agentfuse" / "private.py").write_text("SYNTHETIC-UNTRACKED")
    router = PublicRepoTools(tmp_path)
    assert "monitor.py:1:" in router("search_files", {"query": "class CircuitBreakerMonitor"})
    assert "class CircuitBreakerMonitor" in router("read_file", {"path": "agentfuse/monitor.py"})
    for path in (".env", "../outside.py", str(source), "agentfuse/private.py"):
        assert router("read_file", {"path": path}).startswith("ERROR")
    assert "SYNTHETIC" not in router("search_files", {"query": "SYNTHETIC"})
    assert router("write_file", {"path": "agentfuse/monitor.py"}).startswith("ERROR")
    assert router("read_file", {"path": "agentfuse/monitor.py", "max_lines": 10_000}).startswith("ERROR")
    assert source.read_text() == "SYNTHETIC-LOCAL-EDIT"
