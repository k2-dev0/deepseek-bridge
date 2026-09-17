import asyncio
import threading

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
        assert (await manager.wait(task["task_id"], 1000))["status"] == status
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
    assert (repo / "user-edit").read_text() == "keep me"
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
    with pytest.raises(gate.BridgeError):
        await manager.continue_task(task["task_id"], "Resume")
    fresh = await manager.start("Fresh work")
    assert fresh["session_id"] != task["session_id"]
    await manager.shutdown()
    assert (await manager.wait(fresh["task_id"], 0))["status"] == "interrupted"
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
