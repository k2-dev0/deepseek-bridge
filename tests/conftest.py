import importlib
import importlib.util
import json
import os
import subprocess
import threading

import pytest
from deepseek_harness import Notification, RunResult


def bridge(name):
    assert importlib.util.find_spec("deepseek_bridge") is not None, "bridge is not implemented"
    return importlib.import_module("deepseek_bridge." + name)


def final(status="completed", **values):
    data = dict(
        status=status,
        summary="Done",
        tests=["pytest: passed"],
        question=None,
        affected_paths=["example.py"],
        unresolved=[],
    )
    if status == "needs_decision":
        data["question"] = "Choose the expected behavior."
    data.update(values)
    return json.dumps(data)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    # A malformed GIT_CONFIG_* environment breaks git before any repository is read.
    # Repository binding ignores inherited GIT_* variables; fixture setup does too.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env, capture_output=True)
    except subprocess.CalledProcessError as failure:
        # Some worker sandboxes deny creating .git anywhere. Lifecycle tests only
        # need a directory; tests that read repository metadata use server_cwd().
        detail = (failure.stderr or b"").decode("utf-8", "replace")
        if "Operation not permitted" not in detail and "Permission denied" not in detail:
            raise
    return root


@pytest.fixture
def gate(monkeypatch):
    module = bridge("tasks")

    class Gate:
        def __init__(self, workspace, *args, **kwargs):
            self.workspace = workspace
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls = []
            self.response = final()
            self.failure = None
            self.closed = 0

        def run(self, session_id, message, fresh, stop, *args, **kwargs):
            self.calls.append((session_id, message, fresh))
            self.entered.set()
            assert self.release.wait(5), "test did not release worker"
            if self.failure:
                raise self.failure
            return RunResult(session_id, self.response, "completed", [], [])

        def close(self):
            self.closed += 1
            self.release.set()

    monkeypatch.setattr(module, "Runtime", Gate)
    return module


@pytest.fixture
def sdk_gate(monkeypatch, tmp_path):
    """Real Runtime seam whose SDK session exposes on_notification.

    The fixture replaces only DeepSeekHarness, mirroring the signal test seam:
    Runtime still wires the notification callback, TaskManager still owns task
    state, and the test drives real session.event payloads.
    """

    runtime = bridge("runtime")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sdk-gate-env-key")
    monkeypatch.setattr(
        bridge("privacy"), "user_state_path", lambda *args, **kwargs: tmp_path / "state"
    )

    class Controller:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.closed = 0
            self.session_id = None
            self.on_notification = None
            self.response = final()
            self.finish_reason = "completed"
            self.events = []

        def notify(self, event_type="turn/start", canary=None):
            assert self.on_notification is not None, "Runtime must pass SDK on_notification"
            event = {"type": event_type, "data": {"turn": 1, "step": 1}}
            if canary is not None:
                event["data"]["message"] = canary
            self.on_notification(
                Notification(
                    method="session.event",
                    payload={"sessionId": self.session_id, "event": event},
                )
            )

    controller = Controller()

    class Session:
        def run(self, prompt, *, on_notification=None):
            controller.on_notification = on_notification
            controller.entered.set()
            assert controller.release.wait(5), "test did not release SDK session"
            return RunResult(
                controller.session_id,
                controller.response,
                controller.finish_reason,
                controller.events,
                [],
            )

    class Client:
        _proc = None

    class Harness:
        def __init__(self, **kwargs):
            self.client = Client()

        def start(self):
            pass

        def start_session(self, session_id):
            controller.session_id = session_id
            return Session()

        def close(self):
            controller.closed += 1
            controller.release.set()

    monkeypatch.setattr(runtime, "DeepSeekHarness", Harness)
    return controller
