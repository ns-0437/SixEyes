"""Pilot acceptance: actual adapter, selected exports, no network or raw files."""

import json
import socket
import sys

import pytest

from pilots.agentfuse.demo import capture_demo, load_agentfuse
from pilots.agentfuse.fake_client import FakeChatCompletion, FakeChoice, FakeMessage, ScriptedClient
from pilots.agentfuse.report import analyze_captured_calls


@pytest.fixture
def offline_adapter(monkeypatch):
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def internal_socketpair(*args, **kwargs):
        # Windows implements asyncio's wakeup pipe using a loopback connection.
        # Permit only creation of that stdlib socket pair, not arbitrary connects.
        with monkeypatch.context() as local:
            local.setattr(socket.socket, "connect", original_connect)
            return original_socketpair(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline pilot attempted network access")

    monkeypatch.setattr(socket, "socketpair", internal_socketpair)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)
    # Without the optional dependency, only real-adapter acceptance tests skip.
    # An explicit invalid checkout is an error, never a silent green skip.
    import os
    try:
        load_agentfuse()
    except RuntimeError:
        if os.environ.get("AGENTFUSE_PATH"):
            raise
        pytest.skip("Install requirements-pilot.txt to run actual AgentFuse acceptance")


@pytest.mark.parametrize("scenario,expected", [
    ("stable", ["none"]), ("system-drift", ["system_changed"]),
    ("restart", ["none", "messages_changed"]),
])
async def test_real_adapter_demo_scenarios_have_expected_evidence(
    scenario, expected, offline_adapter, tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    captured = capture_demo(scenario)
    report = await analyze_captured_calls(captured)
    assert [row["change"] for row in report.to_dict()["comparisons"]] == expected
    assert not list(tmp_path.iterdir())
    # Report is reproducible despite timestamps and a fresh ephemeral HMAC key.
    assert (await analyze_captured_calls(captured)).to_dict() == report.to_dict()


async def test_export_excludes_raw_content_names_identifiers_and_timestamps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret = "SYNTHETIC-SECRET-DO-NOT-EXPORT"
    client = ScriptedClient([
        FakeChatCompletion((FakeChoice(FakeMessage(content=secret)),)),
        FakeChatCompletion((FakeChoice(FakeMessage(content=secret)),)),
    ])
    kwargs = {
        "model": secret,
        "messages": [{"role": "system", "content": secret}, {"role": "user", "content": secret}],
        "tools": [{"type": "function", "function": {"name": secret, "parameters": {"title": secret}}}],
        "tool_choice": {"type": "function", "function": {"name": secret}},
    }
    client.chat.completions.create(**kwargs)
    kwargs["messages"][0]["content"] += " changed"
    client.chat.completions.create(**kwargs)
    report = await analyze_captured_calls(client.captured)
    assert report.to_dict()["comparisons"][0]["change"] == "system_changed"
    assert secret not in json.dumps(report.to_dict()) + report.render_text()
    assert str(client.captured[0].captured_at) not in json.dumps(report.to_dict())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("count", [0, 1])
async def test_small_trace_reports_insufficient_evidence(count):
    client = ScriptedClient([FakeChatCompletion((FakeChoice(FakeMessage(content="done")),))])
    if count:
        client.chat.completions.create(model="m", messages=[{"role": "system", "content": "s"}], tools=[])
    report = await analyze_captured_calls(client.captured)
    assert report.to_dict()["comparison_count"] == 0
    assert "Insufficient requests" in report.render_text()


def test_cli_emits_parseable_json_without_adapter_logging(offline_adapter, monkeypatch, capsys):
    from pilots.agentfuse.__main__ import main
    monkeypatch.setattr(sys, "argv", ["pilot", "--scenario", "system-drift", "--format", "json"])
    assert main() == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert data["mode"] == "synthetic_offline"
    assert data["changed_pair_count"] == 1
    assert not output.err


def test_invalid_explicit_checkout_exits_with_actionable_error(monkeypatch, tmp_path, capsys):
    from pilots.agentfuse.__main__ import main
    monkeypatch.setenv("AGENTFUSE_PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["pilot"])
    assert main() == 2
    assert "checkout root" in capsys.readouterr().err
