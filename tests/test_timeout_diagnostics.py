"""Advance the bridge clock without changing asyncio's real scheduling clock."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from conftest import bridge
from deepseek_harness import Notification


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(bridge("tasks"), "time", SimpleNamespace(monotonic=lambda: value[0]))
    return value


async def emit(sdk, *events):
    for event in events:
        sdk.notify(event, canary="private-payload-canary")
    # Flush the real Runtime callback's scheduled activity drain.
    await asyncio.sleep(0)


async def advance(manager, task_id, clock, seconds):
    clock[0] += seconds
    # Real asyncio timers do not advance with the injected bridge clock.
    # Re-arm the actual watchdog at the new time, exercising its deadline path.
    task = manager._tasks[task_id]
    manager._stop_watchdog(task)
    task.watchdog = asyncio.create_task(manager._watch(task))
    await asyncio.sleep(0)


async def start(manager, sdk):
    accepted = await manager.start("Verify bounded waiting")
    assert await asyncio.to_thread(sdk.entered.wait, 2)
    await asyncio.sleep(0)
    return accepted["task_id"]


def diagnostics(result):
    bridge("protocol").WaitOutput.model_validate_json(json.dumps(result))
    message = result["error"]["message"]
    assert len(message) <= 500
    assert "private-payload-canary" not in json.dumps(result)
    return dict(part.strip().split("=", 1) for part in message.split(";")[1:] if "=" in part)


async def test_model_wait_survives_ordinary_deadline(sdk_gate, repo, clock):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, "step/start", "system/message", "user/message")
        before = await manager.wait(task_id, 0)
        await advance(manager, task_id, clock, 599)
        live = await manager.wait(task_id, 10)
        assert live["status"] == "running"
        assert live["last_activity_at"] == before["last_activity_at"]
        sdk_gate.release.set()
        assert (await manager.wait(task_id, 1000))["status"] == "completed"
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


@pytest.mark.parametrize(
    ("events", "seconds", "kind", "waiting", "limit"),
    [
        (("step/start",), 600, "inactivity", "model", 600),
        (("step/start", "system/message", "user/message"), 600, "inactivity", "model", 600),
        (("step/start", "assistant/attempt"), 600, "inactivity", "model", 600),
        (("step/start", "assistant/message", "tool/call"), 300, "inactivity", "activity", 300),
        (("step/start", "tool/result"), 300, "inactivity", "activity", 300),
        (("step/start", "step/end"), 300, "inactivity", "activity", 300),
        (("step/start", "turn/end"), 300, "inactivity", "activity", 300),
        (("assistant/attempt",), 300, "inactivity", "activity", 300),
        ((), 300, "inactivity", "activity", 300),
        (("step/start",), 1200, "hard", "model", 1200),
    ],
)
async def test_timeout_captures_pre_stop_state(
    sdk_gate, repo, clock, events, seconds, kind, waiting, limit
):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, *events)
        before = await manager.wait(task_id, 0)
        await advance(manager, task_id, clock, seconds)
        result = await manager.wait(task_id, 1000)
        assert result["status"] == "failed"
        assert result["error"]["class"] == "task_timeout_error"
        info = diagnostics(result)
        assert info["timeout"] == kind
        assert info["phase"] == before["phase"]
        assert info["last_activity_at"] == before["last_activity_at"]
        assert float(info["inactivity_seconds"]) == seconds
        assert float(info["deadline_seconds"]) == limit
        assert info["waiting_for"] == waiting
        assert result["phase"] == "failed"  # Existing terminal contract.
        await emit(sdk_gate, "tool/result")
        clock[0] += 40
        assert (await manager.wait(task_id, 0))["error"] == result["error"]
        assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
        assert (await manager.start("Fresh task after recovery"))["status"] == "running"
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_activity_boundary_running_at_299_and_timeout_at_300(sdk_gate, repo, clock):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, "tool/call")
        before = await manager.wait(task_id, 0)
        await advance(manager, task_id, clock, 299)
        live = await manager.wait(task_id, 10)
        assert live["status"] == "running"
        assert live["last_activity_at"] == before["last_activity_at"]
        await advance(manager, task_id, clock, 1)
        result = await manager.wait(task_id, 1000)
        assert result["status"] == "failed"
        assert result["error"]["class"] == "task_timeout_error"
        info = diagnostics(result)
        assert info["timeout"] == "inactivity"
        assert info["phase"] == "tool_call"
        assert float(info["inactivity_seconds"]) == 300
        assert float(info["deadline_seconds"]) == 300
        assert info["waiting_for"] == "activity"
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_hard_limit_with_recent_model_activity(sdk_gate, repo, clock):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        for _ in range(3):
            await emit(sdk_gate, "step/start")
            await advance(manager, task_id, clock, 400)
        result = await manager.wait(task_id, 1000)
        info = diagnostics(result)
        assert info["timeout"] == "hard"
        assert float(info["inactivity_seconds"]) == 400
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_unknown_notifications_cannot_hide_tool_stall(sdk_gate, repo, clock):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, "tool/call")
        before = await manager.wait(task_id, 0)
        clock[0] += 299
        for method, session_id, event_type in (
            ("session.event", sdk_gate.session_id, "heartbeat"),
            ("session.event", "another-session", "step/start"),
            ("session.status", sdk_gate.session_id, "step/start"),
        ):
            sdk_gate.on_notification(
                Notification(
                    method=method,
                    payload={
                        "sessionId": session_id,
                        "event": {"type": event_type},
                    },
                )
            )
        await asyncio.sleep(0)
        assert (await manager.wait(task_id, 0))["last_activity_at"] == before["last_activity_at"]
        await advance(manager, task_id, clock, 1)
        info = diagnostics(await manager.wait(task_id, 1000))
        assert info["phase"] == "tool_call"
        assert float(info["inactivity_seconds"]) == 300
    finally:
        sdk_gate.release.set()
        await manager.shutdown()


async def test_cleanup_failure_retains_timeout_diagnosis(sdk_gate, repo, clock, monkeypatch):
    manager = bridge("tasks").TaskManager(repo)
    original_close = manager.runtime.close

    def fail_close():
        raise RuntimeError("private-payload-canary")

    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, "tool/call")
        monkeypatch.setattr(manager.runtime, "close", fail_close)
        await advance(manager, task_id, clock, 300)
        result = await manager.wait(task_id, 1000)
        assert result["error"]["class"] == "abort_error"
        assert diagnostics(result)["timeout"] == "inactivity"
        with pytest.raises(bridge("protocol").BridgeError):
            await manager.start("Must retain reservation")
    finally:
        monkeypatch.setattr(manager.runtime, "close", original_close)
        sdk_gate.release.set()
        await manager.shutdown()


async def test_continue_clears_model_wait(sdk_gate, repo, clock):
    manager = bridge("tasks").TaskManager(repo)
    try:
        task_id = await start(manager, sdk_gate)
        await emit(sdk_gate, "step/start")
        sdk_gate.release.set()
        assert (await manager.wait(task_id, 1000))["status"] == "completed"
        sdk_gate.release.clear()
        sdk_gate.entered.clear()
        await manager.continue_task(task_id, "Next run")
        assert await asyncio.to_thread(sdk_gate.entered.wait, 2)
        await asyncio.sleep(0)
        await advance(manager, task_id, clock, 300)
        info = diagnostics(await manager.wait(task_id, 1000))
        assert info["waiting_for"] == "activity"
        assert info["phase"] == "run_start"
    finally:
        sdk_gate.release.set()
        await manager.shutdown()
