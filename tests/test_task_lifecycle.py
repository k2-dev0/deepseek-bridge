import asyncio
import threading
from datetime import datetime, timedelta

import pytest
from conftest import final


async def wait_entered(runner):
    assert await asyncio.to_thread(runner.entered.wait, 2)


async def test_start_wait_continue_and_reuse(gate, repo):
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("Inspect the repository")
        assert task["status"] == "running"
        await wait_entered(manager.runtime)
        assert (await manager.wait(task["task_id"], 0))["status"] == "running"
        assert (await manager.wait(task["task_id"], 1))["status"] == "running"
        with pytest.raises(gate.BridgeError):
            await manager.start("A concurrent writer")
        with pytest.raises(gate.BridgeError):
            await manager.continue_task(task["task_id"], "Too soon")
        waiter = asyncio.create_task(manager.wait(task["task_id"], 1000))
        manager.runtime.release.set()
        done = await waiter
        assert done["status"] == "completed"
        assert done["final_response"]["summary"] == "Done"
        assert done["phase"] == "completed"
        assert done["observability"] == "available"
        assert done["last_activity_at"] >= done["started_at"]
        assert await manager.continue_task(task["task_id"], "Follow up") == task
        assert (await manager.wait(task["task_id"], 1000))["status"] == "completed"
        calls = manager.runtime.calls
        assert calls[0][0] == calls[1][0] == task["session_id"]
        assert [call[2] for call in calls] == [True, False]
        fresh = await manager.start("New independent work")
        assert fresh["session_id"] != task["session_id"]
        assert (await manager.wait(fresh["task_id"], 1000))["status"] == "completed"
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("status", ["completed", "needs_decision", "failed"])
async def test_model_status_and_continue_rules(gate, repo, status):
    manager = gate.TaskManager(repo)
    manager.runtime.response = final(status)
    manager.runtime.release.set()
    try:
        task = await manager.start("Implement the change")
        result = await manager.wait(task["task_id"], 1000)
        assert result["status"] == status
        assert result["phase"] == status
        assert result["observability"] == "available"
        assert result["last_activity_at"] >= result["started_at"]
        if status == "failed":
            with pytest.raises(gate.BridgeError):
                await manager.continue_task(task["task_id"], "Retry")
        else:
            assert (await manager.continue_task(task["task_id"], "Proceed"))["status"] == "running"
    finally:
        await manager.shutdown()


async def test_abort_is_idempotent_and_preserves_worktree(gate, repo):
    manager = gate.TaskManager(repo)
    (repo / "user-edit").write_text("keep me")
    task = await manager.start("Work")
    await wait_entered(manager.runtime)
    aborted = await manager.abort(task["task_id"])
    assert aborted["status"] == "aborted"
    assert await manager.abort(task["task_id"]) == aborted
    terminal = await manager.wait(task["task_id"], 0)
    assert terminal["phase"] == "aborted"
    assert terminal["observability"] == "available"
    assert terminal["last_activity_at"] >= terminal["started_at"]
    assert (repo / "user-edit").read_text() == "keep me"
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
    with pytest.raises(gate.BridgeError):
        await manager.continue_task(task["task_id"], "Resume")
    fresh = await manager.start("Fresh work")
    assert fresh["session_id"] != task["session_id"]
    await manager.shutdown()
    interrupted = await manager.wait(fresh["task_id"], 0)
    assert interrupted["status"] == "interrupted"
    assert interrupted["phase"] == "interrupted"
    assert interrupted["observability"] == "available"
    with pytest.raises(gate.BridgeError):
        await manager.start("After shutdown")


async def test_failed_worker_contract_and_unknown_task(gate, repo):
    manager = gate.TaskManager(repo)
    manager.runtime.response = "secret raw invalid response"
    manager.runtime.release.set()
    try:
        task = await manager.start("Work")
        done = await manager.wait(task["task_id"], 1000)
        assert done["status"] == "failed"
        assert done["error"]["class"] == "task_contract_error"
        assert "secret raw" not in str(done)
        with pytest.raises(gate.BridgeError):
            await manager.wait("unknown", 0)
        with pytest.raises(gate.BridgeError):
            await manager.abort(task["task_id"])
        manager.runtime.failure = RuntimeError("DEEPSEEK_API_KEY=do-not-expose")
        task2 = await manager.start("Next task")
        error = await manager.wait(task2["task_id"], 1000)
        assert error["status"] == "failed"
        assert error["error"]["class"] == "internal_error"
        assert "do-not-expose" not in str(error)
    finally:
        await manager.shutdown()


async def test_concurrent_start_and_continue_have_one_winner(gate, repo):
    manager = gate.TaskManager(repo)
    try:
        values = await asyncio.gather(
            manager.start("one"), manager.start("two"), return_exceptions=True
        )
        assert sum(isinstance(v, dict) for v in values) == 1
        task = next(v for v in values if isinstance(v, dict))
        manager.runtime.release.set()
        await manager.wait(task["task_id"], 1000)
        manager.runtime.release.clear()
        values = await asyncio.gather(
            manager.continue_task(task["task_id"], "one"),
            manager.continue_task(task["task_id"], "two"),
            return_exceptions=True,
        )
        assert sum(isinstance(v, dict) for v in values) == 1
    finally:
        await manager.shutdown()


def test_task_timeout_defaults(gate):
    assert gate.HARD_TIMEOUT_SECONDS == 20 * 60
    assert gate.INACTIVITY_TIMEOUT_SECONDS == 120


def utc(value):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    return parsed


async def test_wait_snapshot_contract_and_poll_keeps_activity(gate, repo):
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("Inspect the repository")
        await wait_entered(manager.runtime)
        snap = await manager.wait(task["task_id"], 0)
        assert snap["status"] == "running"
        for key in ("started_at", "last_activity_at", "elapsed_ms", "phase", "observability"):
            assert key in snap, f"wait snapshot is missing {key}"
        assert "progress" not in snap
        assert snap["started_at"] is not None
        assert snap["last_activity_at"] is not None
        started = utc(snap["started_at"])
        activity = utc(snap["last_activity_at"])
        assert activity >= started
        assert type(snap["elapsed_ms"]) is int
        assert snap["elapsed_ms"] >= 0
        assert snap["phase"] == "starting"
        assert snap["observability"] == "unavailable"
        await manager.wait(task["task_id"], 1)
        again = await manager.wait(task["task_id"], 0)
        assert again["started_at"] == snap["started_at"]
        assert again["last_activity_at"] == snap["last_activity_at"]
        assert again["elapsed_ms"] >= snap["elapsed_ms"]
        assert len(manager.runtime.calls) == 1
    finally:
        manager.runtime.release.set()
        await manager.shutdown()


async def test_task_hard_timeout_cleans_up_and_releases_reservation(gate, repo, monkeypatch):
    monkeypatch.setattr(gate, "HARD_TIMEOUT_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(gate, "INACTIVITY_TIMEOUT_SECONDS", 60.0, raising=False)
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("Long running work")
        await wait_entered(manager.runtime)
        result = await manager.wait(task["task_id"], 3000)
        assert result["status"] == "failed"
        assert result["phase"] == "failed"
        assert result["observability"] == "available"
        assert result["last_activity_at"] >= result["started_at"]
        assert result["error"]["class"] == "task_timeout_error"
        assert result["final_response"] is None
        assert manager.runtime.closed >= 1
        assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
        fresh = await manager.start("Fresh work after timeout cleanup")
        assert (await manager.wait(fresh["task_id"], 1000))["status"] == "completed"
    finally:
        manager.runtime.release.set()
        await manager.shutdown()


async def test_task_inactivity_timeout_fires_without_activity(gate, repo, monkeypatch):
    monkeypatch.setattr(gate, "HARD_TIMEOUT_SECONDS", 60.0, raising=False)
    monkeypatch.setattr(gate, "INACTIVITY_TIMEOUT_SECONDS", 0.5, raising=False)
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("Work without observable events")
        await wait_entered(manager.runtime)
        result = await manager.wait(task["task_id"], 3000)
        assert result["status"] == "failed"
        assert result["phase"] == "failed"
        assert result["observability"] == "available"
        assert result["error"]["class"] == "task_timeout_error"
        assert manager.runtime.closed >= 1
    finally:
        manager.runtime.release.set()
        await manager.shutdown()


async def test_continue_resets_run_clock_and_keeps_session(gate, repo):
    manager = gate.TaskManager(repo)
    try:
        task = await manager.start("First run")
        await wait_entered(manager.runtime)
        first = await manager.wait(task["task_id"], 0)
        assert first["status"] == "running"
        manager.runtime.release.set()
        completed = await manager.wait(task["task_id"], 1000)
        assert completed["status"] == "completed"
        for snapshot in (first, completed):
            for key in ("started_at", "last_activity_at", "elapsed_ms", "phase", "observability"):
                assert key in snapshot, f"wait snapshot is missing {key}"
        await asyncio.sleep(1.05)
        manager.runtime.entered.clear()
        manager.runtime.release.clear()
        assert (await manager.continue_task(task["task_id"], "Second run"))["status"] == "running"
        await wait_entered(manager.runtime)
        second = await manager.wait(task["task_id"], 0)
        assert second["status"] == "running"
        assert second["final_response"] is None
        assert second["finish_reason"] is None
        assert second["error"] is None
        assert second["started_at"] > completed["started_at"]
        assert second["last_activity_at"] >= second["started_at"]
        assert second["elapsed_ms"] < 1000
        manager.runtime.release.set()
        assert (await manager.wait(task["task_id"], 1000))["status"] == "completed"
        session_ids = [call[0] for call in manager.runtime.calls]
        assert session_ids == [task["session_id"], task["session_id"]]
        assert [call[2] for call in manager.runtime.calls] == [True, False]
    finally:
        manager.runtime.release.set()
        await manager.shutdown()
