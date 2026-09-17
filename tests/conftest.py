import importlib
import importlib.util
import json
import subprocess
import threading

import pytest
from deepseek_harness import RunResult


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
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def gate(monkeypatch):
    module = bridge("tasks")

    class Gate:
        def __init__(self, workspace):
            self.workspace = workspace
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls = []
            self.response = final()
            self.failure = None
            self.closed = 0

        def run(self, session_id, message, fresh, stop):
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
