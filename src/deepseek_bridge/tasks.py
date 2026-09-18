"""Single-writer task lifecycle, condition waiting and coordinated shutdown."""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from deepseek_harness import RunResult
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
# SDK 0.1.5rc1 does not forward agent/assistant-stream frames over its RPC.
# Keep model waiting bounded without treating an invisible stream as a tool stall.
MODEL_WAIT_TIMEOUT_SECONDS = 600
# Fixed bounds for graceful close, owned-process force stop and joins.
CLEANUP_GRACE_SECONDS = 5.0
CLEANUP_JOIN_SECONDS = 5.0
CLEANUP_FORCE_SECONDS = 5.0
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


class _RuntimeCall[T]:
    """Own a blocking SDK call without registering an interpreter-exit join.

    A failed cleanup retains this handle and the writer reservation. Daemon
    status only permits CLI exit after abort_error; success still requires the
    actual thread to finish and be joined.
    """

    def __init__(self, operation: Callable[[], T], name: str):
        loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        self._value: T | None = None
        self._error: BridgeError | None = None

        def invoke() -> None:
            try:
                self._value = operation()
            except BridgeError as error:
                self._error = BridgeError(error.category)
            except BaseException:
                # Never let a background thread print an untrusted traceback.
                self._error = BridgeError("internal_error")
            finally:
                with suppress(RuntimeError):  # The CLI may already have exited.
                    loop.call_soon_threadsafe(self._done.set)

        self.thread = threading.Thread(target=invoke, name=name, daemon=True)
        self.thread.start()

    async def wait(self, timeout: float | None = None) -> T:  # noqa: ASYNC109
        # This owned call applies one asyncio.timeout to completion AND join.
        async with asyncio.timeout(timeout):
            await self._done.wait()
            # The notification is sent just before the thread returns. Verify
            # thread termination as well, without blocking the event loop.
            while self.thread.is_alive():  # noqa: ASYNC110 -- no async Thread.join API
                await asyncio.sleep(0.001)
            self.thread.join(timeout=0)
        if self._error is not None:
            raise self._error
        return cast(T, self._value)


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
    waiting_for: Literal["model", "activity"] = "activity"
    # Frozen text of fixed enums, numbers and bridge-generated time only. Kept
    # separately from error so cleanup's abort_error cannot erase the cause.
    timeout_diagnostics: str | None = None

    @property
    def inactivity_limit(self) -> float:
        return (
            MODEL_WAIT_TIMEOUT_SECONDS
            if self.waiting_for == "model"
            else INACTIVITY_TIMEOUT_SECONDS
        )

    def accepted(self) -> dict[str, str]:
        return {"task_id": self.task_id, "session_id": self.session_id, "status": self.status}

    def snapshot(self) -> dict[str, Any]:
        if self.status == "running":
            elapsed_ms = max(0, int((time.monotonic() - self.run_started_monotonic) * 1000))
        else:
            elapsed_ms = self.elapsed_ms
        error = self.error.as_dict() if self.error else None
        if error is not None and self.timeout_diagnostics is not None:
            error["message"] += self.timeout_diagnostics
        return {
            **self.accepted(),
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "elapsed_ms": elapsed_ms,
            "phase": self.phase,
            "observability": self.observability,
            "final_response": self.final_response.model_dump() if self.final_response else None,
            "finish_reason": self.finish_reason,
            "error": error,
        }


class TaskManager:
    def __init__(self, workspace: Path):
        self.runtime = Runtime(workspace)
        self._tasks: dict[str, Task] = {}
        self._active: Task | None = None
        self._condition = asyncio.Condition()
        self._cleanup = asyncio.Lock()
        self._run_call: _RuntimeCall[RunResult] | None = None
        self._closed = False
        self._poisoned = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._activity_lock = threading.Lock()
        self._activity_queue: list[tuple[Task, str]] = []
        self._pending_notify: set[asyncio.Task[None]] = set()
        self._close_call: _RuntimeCall[None] | None = None
        self._force_call: _RuntimeCall[bool] | None = None

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
        if activity == "step_start":
            task.waiting_for = "model"
        elif activity in {
            "process_start",
            "run_start",
            "turn_start",
            "turn_end",
            "step_end",
            "assistant_message",
            "tool_call",
            "tool_result",
        }:
            task.waiting_for = "activity"
        # system/user messages may follow step/start before the model request.
        # assistant/attempt settles a failed attempt; it is not a start signal.
        task.observability = (
            "unavailable"
            if task.waiting_for == "model" or activity in UNOBSERVABLE_ACTIVITY
            else "available"
        )

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
        task.waiting_for = "activity"
        task.timeout_diagnostics = None
        task.worker = None
        task.watchdog = None

    def _schedule(self, task: Task, message: str, fresh: bool) -> None:
        self._active = task
        self._loop = asyncio.get_running_loop()
        self._reset_run(task)
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

    async def wait(self, task_id: str, timeout_ms: int | None = None) -> dict[str, Any]:
        try:
            WaitInput(task_id=task_id)
        except ValidationError:
            raise BridgeError("configuration_error") from None
        if timeout_ms is not None and (type(timeout_ms) is not int or not 0 <= timeout_ms <= 60000):
            raise BridgeError("configuration_error")
        async with self._condition:
            task = self._lookup(task_id)
            if task.status == "running" and timeout_ms is None:
                await self._condition.wait_for(self._terminal_predicate(task))
            elif task.status == "running" and timeout_ms:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(self._terminal_predicate(task)),
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
            self._run_call = _RuntimeCall(
                lambda: self.runtime.run(task.session_id, message, fresh, task.stop, activity),
                "deepseek-worker-run",
            )
            result = await self._run_call.wait()
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
            async with self._cleanup:
                if task.stopping:
                    return
                try:
                    await self._close_runtime()
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
                self._run_call = None
                self._active = None

    @staticmethod
    def _wait_predicate(task: Task, sequence: int) -> Callable[[], bool]:
        return lambda: task.status != "running" or task.activity_seq != sequence

    @staticmethod
    def _terminal_predicate(task: Task) -> Callable[[], bool]:
        return lambda: task.status != "running"

    @staticmethod
    def _deadline_remaining(task: Task, now: float) -> float:
        return min(
            task.run_started_monotonic + HARD_TIMEOUT_SECONDS - now,
            task.last_activity_monotonic + task.inactivity_limit - now,
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
                now = time.monotonic()
                if self._deadline_remaining(task, now) > 0:
                    return False
                hard = now >= task.run_started_monotonic + HARD_TIMEOUT_SECONDS
                limit = HARD_TIMEOUT_SECONDS if hard else task.inactivity_limit
                task.timeout_diagnostics = (
                    f"; timeout={'hard' if hard else 'inactivity'}"
                    f"; phase={task.phase}"
                    f"; last_activity_at={task.last_activity_at}"
                    f"; inactivity_seconds={max(0.0, now - task.last_activity_monotonic):.3f}"
                    f"; deadline_seconds={limit:g}"
                    f"; waiting_for={task.waiting_for}"
                )
                task.stopping = True
                task.stop.set()
                task.error = BridgeError("task_timeout_error")
                self._condition.notify_all()
            # Keep _cleanup across the whole timeout recovery so abort/shutdown
            # cannot start a second recovery for the same task.
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

    async def _close_runtime(self) -> None:
        process = self.runtime.owned_process()
        # Retain unfinished calls across failed recovery attempts. They are
        # never cancelled or duplicated and cannot block asyncio.run()/atexit.
        if self._close_call is None:
            self._close_call = _RuntimeCall(self.runtime.close, "deepseek-worker-close")
        close = self._close_call
        try:
            timed_out = False
            try:
                await close.wait(CLEANUP_GRACE_SECONDS)
            except TimeoutError:
                timed_out = True
            if timed_out or self._force_call is not None:
                if self._force_call is None:
                    self._force_call = _RuntimeCall(
                        self.runtime.force_stop, "deepseek-worker-force-stop"
                    )
                if not await self._force_call.wait(CLEANUP_FORCE_SECONDS):
                    raise BridgeError("abort_error")
                await close.wait(CLEANUP_JOIN_SECONDS)
        finally:
            if not close.thread.is_alive():
                self._close_call = None
            if self._force_call is not None and not self._force_call.thread.is_alive():
                self._force_call = None
        if process is not None and process.poll() is None:
            raise BridgeError("abort_error")

    async def _recover_resources(self, task: Task | None) -> None:
        await self._close_runtime()
        if self._run_call is not None:
            # Closing a running SDK normally makes run raise. Its result may
            # fail, but its actual thread must be joined before releasing it.
            with suppress(BridgeError):
                await self._run_call.wait(CLEANUP_JOIN_SECONDS)
        if task is not None and task.worker is not None:
            done, _ = await asyncio.wait({task.worker}, timeout=CLEANUP_JOIN_SECONDS)
            if not done:
                raise BridgeError("abort_error")
            try:
                task.worker.result()
            except Exception:
                raise BridgeError("abort_error") from None
        self._run_call = None

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
                # cleanup, and an idle runtime must be reclaimed too.
                # Reuse a stuck pending close instead of starting a second one;
                # publish reclamation only after the bounded recovery succeeds.
                try:
                    await self._recover_resources(task)
                except Exception:
                    self._poisoned = True
                    raise BridgeError("abort_error") from None
                async with self._condition:
                    self._active = None
