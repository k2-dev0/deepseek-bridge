"""Single-writer task lifecycle, condition waiting and coordinated shutdown."""

import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .protocol import (
    AbortInput,
    BridgeError,
    ContinueInput,
    FinalResponse,
    StartInput,
    Status,
    WaitInput,
    parse_final,
)
from .runtime import Runtime


@dataclass
class Task:
    task_id: str
    session_id: str
    status: Status = "running"
    final_response: FinalResponse | None = None
    finish_reason: str | None = None
    error: BridgeError | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    stopping: bool = False
    worker: asyncio.Task[None] | None = None

    def accepted(self) -> dict[str, str]:
        return {"task_id": self.task_id, "session_id": self.session_id, "status": self.status}

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.accepted(),
            "progress": "Harness running" if self.status == "running" else None,
            "final_response": self.final_response.model_dump() if self.final_response else None,
            "finish_reason": self.finish_reason,
            "error": self.error.as_dict() if self.error else None,
        }


class TaskManager:
    def __init__(self, workspace: Path):
        self.runtime = Runtime(workspace)
        self._tasks: dict[str, Task] = {}
        self._active: Task | None = None
        self._condition = asyncio.Condition()
        self._cleanup = asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        self._poisoned = False

    def _transition(self, task: Task, status: Status) -> None:
        allowed = {
            "running": {"completed", "needs_decision", "failed", "aborted", "interrupted"},
            "completed": {"running"},
            "needs_decision": {"running"},
        }
        if status not in allowed.get(task.status, set()):
            raise BridgeError("configuration_error")
        task.status = status
        self._condition.notify_all()

    def _lookup(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise BridgeError("configuration_error")
        return self._tasks[task_id]

    def _require_writer(self) -> None:
        if self._closed or self._poisoned or self._active is not None:
            raise BridgeError("configuration_error")

    def _schedule(self, task: Task, message: str, fresh: bool) -> None:
        self._active = task
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-worker")
        task.worker = asyncio.create_task(self._execute(task, message, fresh))

    async def start(self, brief: str, title: str | None = None) -> dict[str, str]:
        try:
            value = StartInput(brief=brief, title=title)
        except ValidationError:
            raise BridgeError("configuration_error") from None
        async with self._condition:
            self._require_writer()
            task = Task("task-" + uuid.uuid4().hex, "session-" + uuid.uuid4().hex)
            self._tasks[task.task_id] = task
            # title is display metadata, never duplicated into durable bridge state.
            self._schedule(task, value.brief, True)
            return task.accepted()

    async def wait(self, task_id: str, timeout_ms: int = 60000) -> dict[str, Any]:
        try:
            WaitInput(task_id=task_id, timeout_ms=timeout_ms)
        except ValidationError:
            raise BridgeError("configuration_error") from None
        async with self._condition:
            task = self._lookup(task_id)
            if task.status == "running" and timeout_ms:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: task.status != "running"),
                        timeout_ms / 1000,
                    )
                except TimeoutError:
                    pass  # A wait deadline says nothing about the task outcome.
            return task.snapshot()

    async def continue_task(self, task_id: str, message: str) -> dict[str, str]:
        try:
            ContinueInput(task_id=task_id, message=message)
        except ValidationError:
            raise BridgeError("configuration_error") from None
        async with self._condition:
            self._require_writer()
            task = self._lookup(task_id)
            self._transition(task, "running")
            task.final_response = None
            task.finish_reason = None
            task.error = None
            self._schedule(task, message, False)
            return task.accepted()

    async def _execute(self, task: Task, message: str, fresh: bool) -> None:
        error: BridgeError | None = None
        response: FinalResponse | None = None
        finish_reason = None
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                self._executor, self.runtime.run, task.session_id, message, fresh, task.stop
            )
            finish_reason = result.finish_reason
            response = parse_final(result.final_response)
            if response.status == "failed":
                error = BridgeError("model_error")
        except BridgeError as failure:
            error = BridgeError(failure.category)
        except Exception:
            error = BridgeError("internal_error")
        if task.stopping:
            return
        if error:
            try:
                await asyncio.to_thread(self.runtime.close)
            except Exception:
                self._poisoned = True
                error = BridgeError("abort_error")
        async with self._condition:
            if task.stopping:
                return
            task.error = error
            task.final_response = response
            task.finish_reason = finish_reason
            self._transition(task, "failed" if error else response.status if response else "failed")
            if not self._poisoned:
                self._active = None

    async def _stop(self, task: Task, status: Status) -> None:
        # Called with _cleanup held, but no condition held across blocking work.
        async with self._condition:
            task.stopping = True
            task.stop.set()
        try:
            await asyncio.to_thread(self.runtime.close)
            if task.worker is not None:
                await asyncio.shield(task.worker)
            if self._executor is not None:
                await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
                self._executor = None
        except Exception:
            async with self._condition:
                self._poisoned = True
                task.error = BridgeError("abort_error")
                self._transition(task, "failed")
            raise BridgeError("abort_error") from None
        async with self._condition:
            self._transition(task, status)
            self._active = None

    async def abort(self, task_id: str) -> dict[str, str]:
        try:
            AbortInput(task_id=task_id)
        except ValidationError:
            raise BridgeError("configuration_error") from None
        # A cancelled MCP request must not cancel resource cleanup.
        return await asyncio.shield(self._abort(task_id))

    async def _abort(self, task_id: str) -> dict[str, str]:
        async with self._cleanup:
            async with self._condition:
                task = self._lookup(task_id)
                if task.status == "aborted":
                    return {"task_id": task_id, "status": "aborted"}
                if task.status != "running":
                    raise BridgeError("configuration_error")
                task.stopping = True
                task.stop.set()
            await self._stop(task, "aborted")
            return {"task_id": task_id, "status": "aborted"}

    async def shutdown(self) -> None:
        await asyncio.shield(self._shutdown())

    async def _shutdown(self) -> None:
        async with self._cleanup:
            async with self._condition:
                self._closed = True
                task = self._active
                if task is not None and task.status == "running":
                    task.stopping = True
                    task.stop.set()
            if task is not None and task.status == "running":
                await self._stop(task, "interrupted")
            else:
                await asyncio.to_thread(self.runtime.close)
                if self._executor is not None:
                    await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
                    self._executor = None
