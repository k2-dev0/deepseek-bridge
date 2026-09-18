import asyncio
import json
import logging
import os
import threading

import pytest
from conftest import bridge, final


@pytest.mark.parametrize("failure", [FileNotFoundError("secret path"), RuntimeError("secret init")])
async def test_startup_failures_are_sanitized(tmp_path, monkeypatch, failure):
    runtime = bridge("runtime")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "only-env-canary")
    monkeypatch.setattr(bridge("privacy"), "user_state_path", lambda *a, **k: tmp_path / "state")

    def unavailable():
        raise failure

    monkeypatch.setattr(runtime, "bundled_runtime_path", unavailable)
    manager = bridge("tasks").TaskManager(tmp_path / "repo")
    try:
        task = await manager.start("Exercise runtime initialization failure")
        result = await manager.wait(task["task_id"], 2000)
        assert result["status"] == "failed"
        assert result["error"]["class"] == "harness_start_error"
        assert "secret" not in str(result)
        with pytest.raises(bridge("protocol").BridgeError):
            await manager.continue_task(task["task_id"], "Retry")
    finally:
        await manager.shutdown()


async def test_abort_cleanup_failure_poisoned_writer(gate, repo, monkeypatch):
    manager = gate.TaskManager(repo)
    task = await manager.start("Work")
    assert await asyncio.to_thread(manager.runtime.entered.wait, 2)
    close = manager.runtime.close

    def broken_close():
        raise RuntimeError("credential-like raw close error")

    monkeypatch.setattr(manager.runtime, "close", broken_close)
    with pytest.raises(gate.BridgeError, match="abort_error"):
        await manager.abort(task["task_id"])
    result = await manager.wait(task["task_id"], 0)
    assert result["status"] == "failed"
    assert result["error"]["class"] == "abort_error"
    assert "credential-like" not in str(result)
    with pytest.raises(gate.BridgeError):
        await manager.start("Must not overlap orphaned worker")
    monkeypatch.setattr(manager.runtime, "close", close)
    await manager.shutdown()
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())


async def test_cancelled_wait_leaves_task_running(gate, repo):
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("Work")
        waiter = asyncio.create_task(manager.wait(task["task_id"], 60000))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await manager.wait(task["task_id"], 0))["status"] == "running"
        manager.runtime.release.set()
        assert (await manager.wait(task["task_id"], 1000))["status"] == "completed"
    finally:
        await manager.shutdown()


def test_escaped_secret_in_model_json_is_rejected(monkeypatch):
    p = bridge("protocol")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "escaped-credential-canary")
    text = final(summary="escaped-credential-canary").replace("escaped-", "escaped\\u002d")
    with pytest.raises(p.BridgeError, match="task_contract_error"):
        p.parse_final(text)


def test_privacy_environment_overrides_parent(monkeypatch):
    monkeypatch.setenv("DSH_TELEMETRY_MODE", "FEEDBACK_ONLY")
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "0")
    child = bridge("privacy").child_environment()
    assert child["DSH_TELEMETRY_MODE"] == "DISABLED"
    assert child["DSH_TELEMETRY_DISABLED"] == "1"
    assert child["OTEL_SDK_DISABLED"] == "true"


async def test_abort_during_sdk_initialization_never_runs_a_turn(tmp_path, monkeypatch):
    runtime = bridge("runtime")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "initialization-canary")
    monkeypatch.setattr(bridge("privacy"), "user_state_path", lambda *a, **k: tmp_path / "state")
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Client:
        _proc = None

    class Harness:
        client = Client()

        def __init__(self, **kwargs):
            pass

        def start(self):
            entered.set()
            assert release.wait(3)

        def start_session(self, session_id):
            return self

        def run(self, prompt):
            calls.append("run")
            raise AssertionError("Cancelled initialization must not reach a model turn")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(runtime, "DeepSeekHarness", Harness)
    manager = bridge("tasks").TaskManager(tmp_path / "repo")
    try:
        task = await manager.start("Work")
        assert await asyncio.to_thread(entered.wait, 2)
        abort = asyncio.create_task(manager.abort(task["task_id"]))
        await asyncio.sleep(0.02)
        release.set()
        assert (await asyncio.wait_for(abort, 3))["status"] == "aborted"
        assert calls == ["close"]
    finally:
        release.set()
        await manager.shutdown()


def test_reject_invalid_privacy_patch(monkeypatch):
    privacy = bridge("privacy")
    monkeypatch.setattr(privacy.Path, "read_text", lambda self: "- id: missing\n")
    with pytest.raises(bridge("protocol").BridgeError, match="privacy_configuration_error"):
        privacy.privacy_patch()


def test_cli_normal_files_are_private(tmp_path, monkeypatch):
    server = bridge("server")
    created = tmp_path / "bridge-created-file"

    async def fixture_serve():
        created.write_text("metadata")

    monkeypatch.setattr(server, "serve", fixture_serve)
    previous_mask = os.umask(0o022)
    previous_logging = logging.root.manager.disable
    try:
        server.main()
        assert created.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous_mask)
        logging.disable(previous_logging)


async def test_sdk_event_wakes_wait_and_updates_activity_without_payload_leak(sdk_gate, repo):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task = await manager.start("Inspect the repository")
        assert await asyncio.to_thread(sdk_gate.entered.wait, 2)
        before = await manager.wait(task["task_id"], 0)
        assert before["status"] == "running"
        waiter = asyncio.create_task(manager.wait(task["task_id"], 5000))
        await asyncio.sleep(1.1)
        sdk_gate.notify("turn/start", canary="sdk-notification-canary-9f3")
        woken = await asyncio.wait_for(waiter, 2)
        assert woken["status"] == "running"
        assert woken["observability"] in {"available", "unavailable"}
        assert isinstance(woken["phase"], str) and woken["phase"]
        assert woken["last_activity_at"] is not None
        assert woken["last_activity_at"] > before["last_activity_at"]
        assert "progress" not in woken
        assert "sdk-notification-canary-9f3" not in json.dumps(before)
        assert "sdk-notification-canary-9f3" not in json.dumps(woken)
        sdk_gate.release.set()
        completed = await manager.wait(task["task_id"], 2000)
        assert completed["status"] == "completed"
        assert completed["phase"] == "completed"
        assert completed["observability"] == "available"
        for _ in range(5):
            await asyncio.sleep(0)
        assert manager._activity_queue == []
        assert manager._pending_notify == set()
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_timeout_cleanup_failure_keeps_reservation(gate, repo, monkeypatch):
    monkeypatch.setattr(gate, "HARD_TIMEOUT_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(gate, "INACTIVITY_TIMEOUT_SECONDS", 60.0, raising=False)
    manager = gate.TaskManager(repo)
    task = await manager.start("Work")
    assert await asyncio.to_thread(manager.runtime.entered.wait, 2)
    close = manager.runtime.close

    def broken_close():
        raise RuntimeError("timeout-cleanup-canary")

    monkeypatch.setattr(manager.runtime, "close", broken_close)
    result = await manager.wait(task["task_id"], 3000)
    assert result["status"] == "failed"
    assert result["phase"] == "failed"
    assert result["observability"] == "available"
    assert result["error"]["class"] == "abort_error"
    assert "timeout-cleanup-canary" not in str(result)
    with pytest.raises(gate.BridgeError):
        await manager.start("Must not overlap unreleased writer")
    monkeypatch.setattr(manager.runtime, "close", close)
    await manager.shutdown()
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())


async def test_timeout_shutdown_cleanup_is_serialized(gate, repo, monkeypatch):
    monkeypatch.setattr(gate, "HARD_TIMEOUT_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(gate, "INACTIVITY_TIMEOUT_SECONDS", 60.0, raising=False)
    manager = gate.TaskManager(repo)
    task = await manager.start("Work")
    assert await asyncio.to_thread(manager.runtime.entered.wait, 2)

    original_close = manager.runtime.close
    inside = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0

    def controlled_close():
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            inside.set()
        release.wait(5)
        with guard:
            active -= 1
        original_close()

    monkeypatch.setattr(manager.runtime, "close", controlled_close)
    try:
        assert await asyncio.to_thread(inside.wait, 2)
        shutdown = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(shutdown, 3)
        result = await manager.wait(task["task_id"], 0)
        assert max_active == 1
        assert result["status"] == "failed"
        assert result["error"]["class"] == "task_timeout_error"
        assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
    finally:
        release.set()


async def test_turn_end_transport_failure_is_transport_error(sdk_gate, repo):
    sdk_gate.response = ""
    sdk_gate.finish_reason = "error"
    sdk_gate.events = [
        {
            "type": "turn/end",
            "data": {
                "turn": 1,
                "reason": {
                    "kind": "error",
                    "error": {"code": "TRANSPORT", "message": "transport-canary-detail"},
                },
            },
        }
    ]
    manager = bridge("tasks").TaskManager(repo)
    try:
        task = await manager.start("Exercise transport classification")
        assert await asyncio.to_thread(sdk_gate.entered.wait, 2)
        sdk_gate.release.set()
        result = await manager.wait(task["task_id"], 2000)
        assert result["status"] == "failed"
        assert result["phase"] == "failed"
        assert result["observability"] == "available"
        assert result["error"]["class"] == "transport_error"
        assert "transport-canary-detail" not in json.dumps(result)
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_unobservable_phases_are_strict_and_payload_stays_private(sdk_gate, repo):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task = await manager.start("Inspect the repository")
        assert await asyncio.to_thread(sdk_gate.entered.wait, 2)
        for event_type, phase in (("step/start", "step_start"), ("tool/call", "tool_call")):
            sdk_gate.notify(event_type, canary="phase-payload-canary-7c1")
            snapshot = await manager.wait(task["task_id"], 500)
            assert snapshot["status"] == "running"
            assert snapshot["phase"] == phase
            assert snapshot["observability"] == "unavailable"
            assert "phase-payload-canary-7c1" not in json.dumps(snapshot)
        sdk_gate.notify("tool/result", canary="phase-payload-canary-7c1")
        snapshot = await manager.wait(task["task_id"], 500)
        assert snapshot["phase"] == "tool_result"
        assert snapshot["observability"] == "available"
        assert "phase-payload-canary-7c1" not in json.dumps(snapshot)
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_activity_continuation_defers_inactivity_timeout(sdk_gate, repo, monkeypatch):
    monkeypatch.setattr(bridge("tasks"), "INACTIVITY_TIMEOUT_SECONDS", 0.8)
    monkeypatch.setattr(bridge("tasks"), "HARD_TIMEOUT_SECONDS", 30.0)
    manager = bridge("tasks").TaskManager(repo)
    try:
        task = await manager.start("Keep activity alive")
        assert await asyncio.to_thread(sdk_gate.entered.wait, 2)
        for _ in range(10):
            sdk_gate.notify("tool/result")
            await asyncio.sleep(0.2)
        live = await manager.wait(task["task_id"], 0)
        assert live["status"] == "running"
        assert live["phase"] == "tool_result"
        assert live["error"] is None
        result = await manager.wait(task["task_id"], 3000)
        assert result["status"] == "failed"
        assert result["phase"] == "failed"
        assert result["observability"] == "available"
        assert result["error"]["class"] == "task_timeout_error"
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


def test_common_instructions_batch_reads_without_rereading():
    lowered = bridge("protocol").COMMON_INSTRUCTIONS.lower()
    assert "search" in lowered
    assert "same step" in lowered
    assert "re-read" in lowered or "reread" in lowered
    assert "already read" in lowered
