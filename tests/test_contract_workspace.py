import json
import os
from pathlib import Path

import pytest
from conftest import bridge, final


def test_workspace_binding(repo, monkeypatch, tmp_path):
    runtime = bridge("runtime")
    nested = repo / "nested"
    nested.mkdir()
    assert runtime.bind_workspace(nested) == repo.resolve()
    with pytest.raises(Exception, match="configuration_error"):
        runtime.bind_workspace(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(Exception, match="configuration_error"):
        runtime.bind_workspace(alias)
    monkeypatch.setattr(Path, "home", lambda: repo)
    with pytest.raises(Exception, match="configuration_error"):
        runtime.bind_workspace(repo)
    with pytest.raises(Exception, match="configuration_error"):
        runtime.bind_workspace(Path("/"))


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n\t",
        "x" * 32001,
        "DEEPSEEK_API_KEY=example-secret",
        "sk-" + "a" * 32,
        "Authorization: Bearer value",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_input_rejected(text):
    protocol = bridge("protocol")
    with pytest.raises(ValueError):
        protocol.StartInput(brief=text)
    with pytest.raises(ValueError):
        protocol.ContinueInput(task_id="task-x", message=text)


def test_schema_and_boundaries(monkeypatch):
    p = bridge("protocol")
    assert p.StartInput(brief="x" * 32000, title="x" * 200)
    for values in (
        {"brief": "ok", "title": "x" * 201},
        {"brief": "ok", "workspace": "/"},
        {"brief": "ok", "model": "other"},
        {"brief": "ok", "title": " "},
    ):
        with pytest.raises(ValueError):
            p.StartInput(**values)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unique-canary-secret")
    with pytest.raises(ValueError):
        p.StartInput(brief="include unique-canary-secret please")
    for value in (-1, 60001, True, 1.5, "100"):
        with pytest.raises(ValueError):
            p.WaitInput(task_id="task-x", timeout_ms=value)
    assert p.WaitInput(task_id="task-x", timeout_ms=0).timeout_ms == 0
    assert p.WaitInput(task_id="task-x", timeout_ms=60000).timeout_ms == 60000


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "```json\n{}\n```",
        "{}",
        final("running"),
        final(extra="invalid"),
        final(summary="x" * 2001),
        final(tests=["x"] * 51),
        final(affected_paths=["../secret"]),
        final(question="unnecessary"),
        final(question=" "),
        final("needs_decision", question=None),
        final(unresolved="wrong type"),
        "x" * 16001,
        final().replace('"status": "completed"', '"status": "failed", "status": "completed"'),
    ],
)
def test_strict_final_contract(response):
    p = bridge("protocol")
    with pytest.raises(p.BridgeError, match="task_contract_error"):
        p.parse_final(response)


def test_final_contract_valid_and_sanitized(monkeypatch):
    p = bridge("protocol")
    assert p.parse_final(final()).status == "completed"
    assert p.parse_final(final("needs_decision")).question
    monkeypatch.setenv("DEEPSEEK_API_KEY", "wire-canary-credential")
    with pytest.raises(p.BridgeError) as failure:
        p.parse_final(final(summary="wire-canary-credential"))
    assert "wire-canary" not in str(failure.value)
    assert json.loads(p.parse_final(final()).model_dump_json())["summary"] == "Done"


def test_private_state_and_patch(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    p = bridge("privacy")
    monkeypatch.setattr(p, "user_state_path", lambda *args, **kwargs: tmp_path / "state")
    state = p.prepare_state(repo)
    assert not state.home.is_relative_to(repo)
    assert repo.name not in state.home.name
    assert len(state.home.name) == 64
    for path in (state.home, state.runtime):
        assert path.stat().st_mode & 0o777 == 0o700
    assert "enabled: false" in p.privacy_patch().read_text()
    assert p.child_environment()["DSH_TELEMETRY_MODE"] == "DISABLED"
    assert p.child_environment()["DSH_TELEMETRY_DISABLED"] == "1"
    assert os.environ.get("DSH_TELEMETRY_MODE") != "DISABLED"


def test_missing_key(repo, monkeypatch):
    r = bridge("runtime")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(Exception, match="configuration_error"):
        r.validate_environment()
