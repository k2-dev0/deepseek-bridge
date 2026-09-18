"""Synchronous SDK ownership. TaskManager alone schedules calls to this object."""

import importlib.metadata
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from deepseek_harness import DeepSeekHarness, Notification, RunResult
from deepseek_harness.errors import JsonRpcError, SdkProtocolError, TransportClosedError
from deepseek_harness_runtime import bundled_runtime_path

from .macos_process import runtime_patch
from .privacy import child_environment, prepare_state, privacy_patch
from .protocol import COMMON_INSTRUCTIONS, BridgeError, ErrorClass

SDK_VERSION = "0.1.5rc1"
MODEL = "deepseek-flash"
PROFILE = "sdk-minimal"
# Bounded force-stop values for the captured owned process only.
FORCE_TERMINATE_SECONDS = 2.0
FORCE_KILL_SECONDS = 2.0

# Only fixed activity tokens cross from the SDK callback into TaskManager.
# Event bodies, tool arguments, messages and exception text never leave this module.
_EVENT_ACTIVITY: dict[str, str] = {
    "turn/start": "turn_start",
    "turn/end": "turn_end",
    "step/start": "step_start",
    "step/end": "step_end",
    "tool/call": "tool_call",
    "tool/result": "tool_result",
    "assistant/attempt": "model_attempt",
    "assistant/message": "assistant_message",
    "user/message": "user_message",
    "system/message": "system_message",
}


def notification_activity(notification: Notification, session_id: str) -> str | None:
    """Map one SDK notification to a fixed token, or None for anything unknown."""
    if notification.method != "session.event":
        return None
    payload = notification.payload
    if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    return _EVENT_ACTIVITY.get(event_type)


def bind_workspace(cwd: Path | None = None) -> Path:
    start = cwd if cwd is not None else Path.cwd()
    if cwd is None:
        logical = Path(os.environ.get("PWD", str(start)))
        if logical.is_absolute() and logical.resolve() == start.resolve():
            start = logical
    try:
        result = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(start),
                "rev-parse",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
        raw_root = Path(result.stdout.strip())
        root = raw_root.resolve(strict=True)
        if (
            not raw_root.is_absolute()
            or raw_root.is_symlink()
            or root == Path(root.anchor)
            or root == Path.home().resolve()
        ):
            raise ValueError("Unsafe repository root")
        for ancestor in (start, *start.parents):
            if ancestor.resolve() == root and ancestor.is_symlink():
                raise ValueError("Symlink repository root")
        return root
    except (OSError, ValueError, subprocess.SubprocessError):
        raise BridgeError("configuration_error") from None


def validate_environment() -> None:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key.strip() or "\n" in key or "\r" in key:
        raise BridgeError("configuration_error")
    endpoint = os.environ.get("DEEPSEEK_BASE_URL")
    if endpoint:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BridgeError("configuration_error")
    try:
        for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"):
            if importlib.metadata.version(package) != SDK_VERSION:
                raise BridgeError("configuration_error")
    except importlib.metadata.PackageNotFoundError:
        raise BridgeError("harness_start_error") from None


def classify_model_failure(detail: str) -> ErrorClass:
    """Inspect in memory only; never return remote text, headers or exception repr."""
    value = detail.lower()
    if re.search(r"\b401\b|authentication|unauthorized|invalid.api.key", value):
        return "authentication_error"
    if re.search(r"\btransport\b", value, re.IGNORECASE):
        return "transport_error"
    if re.search(
        r"\b429\b|\b5\d\d\b|rate.limit|overload|fetch failed|network|timeout|connection", value
    ):
        return "transport_error"
    if re.search(r"json|parse|malformed|stream|protocol", value):
        return "harness_protocol_error"
    return "model_error"


def _wait_owned_process(process: Any, timeout: float) -> bool:
    with suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=timeout)
    return process.poll() is not None


class Runtime:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.harness: DeepSeekHarness | None = None
        self._lifecycle = threading.Lock()

    def run(
        self,
        session_id: str,
        message: str,
        fresh: bool,
        stop: threading.Event,
        activity: Callable[[str], None] | None = None,
    ) -> RunResult:
        def report(token: str) -> None:
            if activity is not None:
                with suppress(Exception):
                    activity(token)

        # Serialize lazy initialization with close. Initialization has a finite
        # SDK timeout; a cancellation received during it wins before session.run.
        with self._lifecycle:
            if stop.is_set():
                raise BridgeError("abort_error")
            if self.harness is None:
                validate_environment()
                patch = privacy_patch()
                state = prepare_state(self.workspace)
                try:
                    compatibility = runtime_patch(state.runtime)
                    patches = (str(patch),) + ((str(compatibility),) if compatibility else ())
                    self.harness = DeepSeekHarness(
                        dsh_home=str(state.home),
                        cwd=str(self.workspace),
                        runtime_cwd=str(self.workspace),
                        dsh_bin=str(bundled_runtime_path()),
                        provider="deepseek-official",
                        model=MODEL,
                        reasoning_effort="max",
                        profile=PROFILE,
                        patches=patches,
                        env=child_environment(),
                        initialize_timeout_seconds=30.0,
                        shutdown_timeout_seconds=2.0,
                    )
                    self.harness.start()
                    report("process_start")
                except Exception:
                    # TaskManager will close an instance even after partial startup.
                    raise BridgeError("harness_start_error") from None
            session = self.harness.start_session(session_id)
        if stop.is_set():
            raise BridgeError("abort_error")
        report("run_start")
        prompt = COMMON_INSTRUCTIONS + "\nTask:\n" + message if fresh else message

        def on_notification(notification: Notification) -> None:
            token = notification_activity(notification, session_id)
            if token is not None:
                report(token)

        try:
            # Session.run does not lazily start a runtime. A close between the
            # check above and this call fails the send; it cannot resurrect it.
            result: RunResult = session.run(prompt, on_notification=on_notification)
        except (SdkProtocolError, TransportClosedError):
            raise BridgeError("harness_protocol_error") from None
        except JsonRpcError as error:
            raise BridgeError(classify_model_failure(str(error))) from None
        except (TimeoutError, ConnectionError):
            raise BridgeError("transport_error") from None
        if result.session_id != session_id:
            raise BridgeError("harness_protocol_error")
        if result.finish_reason != "completed":
            endings = [event for event in result.events if event.get("type") == "turn/end"]
            detail = json.dumps(endings[-1].get("data")) if endings else ""
            raise BridgeError(classify_model_failure(detail))
        return result

    def owned_process(self) -> Any:
        """Captured owned Popen. Never waits on the lifecycle lock."""
        harness = self.harness
        return harness.client._proc if harness is not None else None

    def force_stop(self) -> bool:
        """Bound the captured owned process after a stuck graceful close.

        Only this instance's captured Popen is polled/terminated/waited/killed.
        No other process is searched for or killed, and no process output or
        exception text crosses this boundary.
        """
        process = self.owned_process()
        if process is None or process.poll() is not None:
            return True
        with suppress(OSError):
            process.terminate()
        if _wait_owned_process(process, FORCE_TERMINATE_SECONDS):
            return True
        with suppress(OSError):
            process.kill()
        return _wait_owned_process(process, FORCE_KILL_SECONDS)

    def close(self) -> None:
        with self._lifecycle:
            harness = self.harness
            if harness is None:
                return
            # The pinned SDK has no cancellation RPC. close() sends shutdown,
            # then terminate/kill/wait with bounds. Inspect its owned process to
            # verify cleanup, never search for or kill another task's process.
            process = harness.client._proc
            try:
                harness.close()
                if process is not None and process.poll() is None:
                    raise BridgeError("abort_error")
            except Exception:
                raise BridgeError("abort_error") from None
            self.harness = None
