"""Single-writer task lifecycle, condition waiting and coordinated shutdown."""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from .protocol import (
    AbortInput,
    BridgeError,
    ContinueInput,
    FinalResponse,
    Phase,
    StartInput,
    Status,
    WaitInput,
    parse_final,
)
from .runtime import Runtime

HARD_TIMEOUT_SECONDS = 20 * 60
INACTIVITY_TIMEOUT_SECONDS = 120
# Fixed bounds for graceful close, owned-process force stop and joins.
CLEANUP_GRACE_SECONDS = 5.0
CLEANUP_JOIN_SECONDS = 5.0
EXECUTOR_JOIN_SECONDS = 5.0
ACTIVITY_PHASES = frozenset(
    {
        "process_start",
        "run_start",
        "turn_start",
        "turn_end",
        "step_start",
        "step_end",
        "tool_call",
        "tool_result",
        "model_attempt",
        "assistant_message",
        "user_message",
        "system_message",
    }
)
# Phases whose next internal event is the only observable progress.
UNOBSERVABLE_ACTIVITY = frozenset(
    {"starting", "process_start", "run_start", "step_start", "tool_call", "model_attempt"}
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    watchdog: asyncio.Task[None] | None = None
    started_at: str = ""
    last_activity_at: str = ""
    run_started_monotonic: float = 0.0
    last_activity_monotonic: float = 0.0
    elapsed_ms: int = 0
    phase: Phase = "starting"
    observability: Literal["available", "unavailable"] = "unavailable"
    activity_seq: int = 0

    def accepted(self) -> dict[str, str]:
        return {"task_id": self.task_id, "session_id": self.session_id, "status": self.status}

    def snapshot(self) -> dict[str, Any]:
        if self.status == "running":
            elapsed_ms = max(0, int((time.monotonic() - self.run_started_monotonic) * 1000))
        else:
            elapsed_ms = self.elapsed_ms
        return {
            **self.accepted(),
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "elapsed_ms": elapsed_ms,
            "phase": self.phase,
            "observability": self.observability,
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._activity_lock = threading.Lock()
        self._activity_queue: list[tuple[Task, str]] = []
        self._pending_notify: set[asyncio.Task[None]] = set()
        self._pending_close: set[asyncio.Task[None]] = set()

    def _transition(self, task: Task, status: Status) -> None:
        allowed = {
            "running": {"completed", "needs_decision", "failed", "aborted", "interrupted"},
            "completed": {"running"},
            "needs_decision": {"running"},
        }
        if status not in allowed.get(task.status, set()):
            raise BridgeError("configuration_error")
        if status != "running" and task.status == "running":
            now = time.monotonic()
            task.elapsed_ms = max(0, int((now - task.run_started_monotonic) * 1000))
            task.activity_seq += 1
            task.last_activity_monotonic = now
            task.last_activity_at = _utc_now()
            task.phase = cast(Phase, status)
            task.observability = "available"
        task.status = status
        if status != "running":
            self._stop_watchdog(task)
        self._condition.notify_all()

    def _stop_watchdog(self, task: Task) -> None:
        watchdog = task.watchdog
        task.watchdog = None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()

    def _lookup(self, task_id: str) -> Task:
        if task_id not in self._tasks:
            raise BridgeError("configuration_error")
        return self._tasks[task_id]

    def _require_writer(self) -> None:
        if self._closed or self._poisoned or self._active is not None:
            raise BridgeError("configuration_error")

    def _activity_callback(self, task: Task) -> Callable[[str], None]:
        def report(activity: str) -> None:
            loop = self._loop
            if loop is None or loop.is_closed() or task.stopping:
                return
            if activity not in ACTIVITY_PHASES:
                return
            with self._activity_lock:
                self._activity_queue.append((task, activity))
            loop.call_soon_threadsafe(self._drain_activities)

        return report

    def _drain_activities(self) -> None:
        if not self._apply_pending_activities():
            return
        notify = asyncio.create_task(self._notify_waiters())
        self._pending_notify.add(notify)
        notify.add_done_callback(self._pending_notify.discard)

    def _apply_pending_activities(self) -> int:
        with self._activity_lock:
            pending = self._activity_queue
            self._activity_queue = []
        for task, activity in pending:
            self._apply_activity(task, activity)
        return len(pending)

    def _apply_activity(self, task: Task, activity: str) -> None:
        if task.status != "running" or task.stopping:
            return
        task.activity_seq += 1
        task.last_activity_monotonic = time.monotonic()
        task.last_activity_at = _utc_now()
        task.phase = cast(Phase, activity)
        task.observability = "unavailable" if activity in UNOBSERVABLE_ACTIVITY else "available"

    async def _notify_waiters(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    def _reset_run(self, task: Task) -> None:
        now = time.monotonic()
        task.status = "running"
        task.started_at = _utc_now()
        task.last_activity_at = task.started_at
        task.run_started_monotonic = now
        task.last_activity_monotonic = now
        task.elapsed_ms = 0
        task.phase = "starting"
        task.observability = "unavailable"
        task.activity_seq += 1
        task.stop = threading.Event()
        task.stopping = False
        task.final_response = None
        task.finish_reason = None
        task.error = None
        task.worker = None
        task.watchdog = None

    def _schedule(self, task: Task, message: str, fresh: bool) -> None:
        self._active = task
        self._loop = asyncio.get_running_loop()
        self._reset_run(task)
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-worker")
        task.worker = asyncio.create_task(self._execute(task, message, fresh))
        task.watchdog = asyncio.create_task(self._watch(task))

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
            sequence = task.activity_seq
            if task.status == "running" and timeout_ms:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(self._wait_predicate(task, sequence)),
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
        activity = self._activity_callback(task)
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                self.runtime.run,
                task.session_id,
                message,
                fresh,
                task.stop,
                activity,
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
            # Drain the last executor activity before publishing the terminal state.
            self._apply_pending_activities()
            task.error = error
            task.final_response = response
            task.finish_reason = finish_reason
            self._transition(task, "failed" if error else response.status if response else "failed")
            if not self._poisoned:
                self._active = None

    @staticmethod
    def _wait_predicate(task: Task, sequence: int) -> Callable[[], bool]:
        return lambda: task.status != "running" or task.activity_seq != sequence

    @staticmethod
    def _deadline_remaining(task: Task, now: float) -> float:
        return min(
            task.run_started_monotonic + HARD_TIMEOUT_SECONDS - now,
            task.last_activity_monotonic + INACTIVITY_TIMEOUT_SECONDS - now,
        )

    async def _watch(self, task: Task) -> None:
        try:
            while True:
                async with self._condition:
                    if task.status != "running" or task is not self._active:
                        return
                    sequence = task.activity_seq
                    remaining = self._deadline_remaining(task, time.monotonic())
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(
                                self._condition.wait_for(self._wait_predicate(task, sequence)),
                                remaining,
                            )
                        except TimeoutError:
                            pass
                        else:
                            continue
                    # Deadline reached: drain queued activity and re-evaluate the
                    # latest activity/deadlines under the condition lock.
                    if self._apply_pending_activities():
                        self._condition.notify_all()
                    if task.status != "running" or task.stopping or task is not self._active:
                        return
                    if self._deadline_remaining(task, time.monotonic()) > 0:
                        continue
                if not await self._timeout(task):
                    continue
                return
        except asyncio.CancelledError:
            return

    async def _timeout(self, task: Task) -> bool:
        async with self._cleanup:
            async with self._condition:
                if task.status != "running" or task.stopping or task is not self._active:
                    return True
                if self._apply_pending_activities():
                    self._condition.notify_all()
                if self._deadline_remaining(task, time.monotonic()) > 0:
                    return False
                task.stopping = True
                task.stop.set()
                task.error = BridgeError("task_timeout_error")
                self._condition.notify_all()
            # Keep _cleanup across the whole timeout recovery so abort/shutdown
            # cannot start a second close/executor shutdown for the same task.
            with suppress(BridgeError):
                await self._stop(task, "failed")
        return True

    async def _stop(self, task: Task, status: Status) -> None:
        # Called with _cleanup held, but no condition held across blocking work.
        async with self._condition:
            task.stopping = True
            task.stop.set()
        try:
            await self._recover_resources(task)
        except Exception:
            async with self._condition:
                self._poisoned = True
                task.error = BridgeError("abort_error")
                self._transition(task, "failed")
            raise BridgeError("abort_error") from None
        async with self._condition:
            self._transition(task, status)
            self._active = None

    async def _recover_resources(self, task: Task | None) -> None:
        # The graceful close may block indefinitely on a stuck SDK pipe write;
        # every wait below is bounded, and a pending close task is never cancelled.
        process = self.runtime.owned_process()
        # Reuse an unfinished close task: its stuck thread may still own the
        # runtime lifecycle lock, so starting a second close would only block.
        close_task = next((pending for pending in self._pending_close if not pending.done()), None)
        if close_task is None:
            close_task = asyncio.create_task(asyncio.to_thread(self.runtime.close))
            self._pending_close.add(close_task)
            close_task.add_done_callback(self._pending_close.discard)
        done, _ = await asyncio.wait({close_task}, timeout=CLEANUP_GRACE_SECONDS)
        if not done:
            # Force-stop only the captured owned process so close can unblock,
            # then join the close task with a second fixed bound.
            forced = await asyncio.to_thread(self.runtime.force_stop)
            if not forced:
                raise BridgeError("abort_error")
            done, _ = await asyncio.wait({close_task}, timeout=CLEANUP_JOIN_SECONDS)
        if not done:
            raise BridgeError("abort_error")
        if close_task.exception() is not None:
            raise BridgeError("abort_error")
        if process is not None and process.poll() is None:
            raise BridgeError("abort_error")
        if task is not None and task.worker is not None:
            done, _ = await asyncio.wait({task.worker}, timeout=CLEANUP_JOIN_SECONDS)
            if not done:
                raise BridgeError("abort_error")
            try:
                task.worker.result()
            except Exception:
                raise BridgeError("abort_error") from None
        if self._executor is not None:
            # executor.shutdown(wait=True) runs only after worker termination.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True),
                    EXECUTOR_JOIN_SECONDS,
                )
            except Exception:
                raise BridgeError("abort_error") from None
            self._executor = None

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
                if task.stopping:
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
                # A terminal task may still hold the reservation after a failed
                # cleanup, and an idle runtime/executor must be reclaimed too.
                # Reuse a stuck pending close instead of starting a second one;
                # publish reclamation only after the bounded recovery succeeds.
                await self._recover_resources(task)
                async with self._condition:
                    self._active = None
