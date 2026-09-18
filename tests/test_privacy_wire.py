"""Real SDK + bundled runtime; HTTP fixtures replace only the remote provider."""

import asyncio
import errno
import importlib.metadata
import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import bridge, final
from deepseek_harness import DeepSeekHarness

pytestmark = pytest.mark.wire
KEY = "wire-only-canary-" + "a" * 24


async def wait_terminal(manager, task_id, timeout_seconds):
    """Return the first terminal snapshot; running activity is intermediate."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        result = await manager.wait(task_id, min(remaining_ms, 60000))
        if result["status"] != "running":
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"task stayed running until the deadline: {result}")


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _instruction_counts(request, instructions):
    system_count = 0
    user_count = 0
    for message in request["messages"]:
        role = message.get("role")
        if role not in ("system", "user"):
            continue
        occurrences = _message_text(message).count(instructions)
        if role == "system":
            system_count += occurrences
        else:
            user_count += occurrences
    return system_count, user_count


@pytest.fixture
def endpoint():
    captured = {
        "requests": [],
        "otel": [],
        "mode": "ok",
        "arrived": threading.Event(),
        "release": threading.Event(),
        "authorization": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path != "/chat/completions":
                captured["otel"].append(self.path)
                self.send_response(200)
                self.end_headers()
                return
            body = json.loads(raw)
            captured["requests"].append(body)
            captured["authorization"].append(self.headers.get("Authorization") == "Bearer " + KEY)
            captured["arrived"].set()
            mode = captured["mode"]
            if mode == "hang":
                captured["release"].wait(15)
                return
            if isinstance(mode, int):
                self.send_response(mode)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"error": {"message": "test failure", "code": mode}}).encode()
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if mode == "malformed":
                self.wfile.write(b"data: {malformed}\n\ndata: [DONE]\n\n")
                return
            chunk = {
                "id": "wire-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": final()},
                        "finish_reason": None,
                    }
                ],
            }
            tool_turn = mode in ("tool", "slowtool") and not any(
                message.get("role") == "tool" for message in body["messages"]
            )
            if tool_turn:
                tool = body["tools"][0]["function"]
                arguments = json.dumps({"command": captured["command"]})
                chunk["choices"][0]["delta"] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "fixture-call",
                            "type": "function",
                            "function": {"name": tool["name"], "arguments": arguments},
                        }
                    ],
                }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            chunk["choices"] = [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_turn else "stop"}
            ]
            chunk["usage"] = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    captured["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield captured
    finally:
        captured["release"].set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def wire_env(endpoint, monkeypatch, tmp_path):
    p = bridge("privacy")
    monkeypatch.setattr(p, "user_state_path", lambda *args, **kwargs: tmp_path / "state")
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", endpoint["url"])
    monkeypatch.setenv("DSH_TELEMETRY_MODE", "FEEDBACK_ONLY")
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "0")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint["url"] + "/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint["url"] + "/v1/logs")
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    return workspace


def test_real_wire_privacy_and_unpatched_difference(endpoint, wire_env, capfd, tmp_path):
    runtime = bridge("runtime")
    runner = runtime.Runtime(wire_env)
    try:
        first = runner.run(
            "session-" + uuid.uuid4().hex, "WIRE_CANARY normal input", True, threading.Event()
        )
        first_process = runner.harness.client._proc
        assert first.finish_reason == "completed"
        assert json.loads(first.final_response)["status"] == "completed"
        safe = endpoint["requests"][0]
        assert set(safe) == {
            "max_tokens",
            "messages",
            "model",
            "reasoning_effort",
            "stream",
            "stream_options",
            "thinking",
            "tools",
        }
        assert safe["model"] == "deepseek-flash"
        assert safe["reasoning_effort"] == "max"
        assert "dsh_session_log" not in safe
        assert "dsh_plugin_packages" not in safe
        assert "WIRE_CANARY" in json.dumps(safe["messages"])
        assert KEY not in json.dumps(safe)
        assert endpoint["authorization"] == [True]
        assert "AGENTS.md" in json.dumps(safe["messages"])
        continue_index = len(endpoint["requests"])
        second = runner.run(first.session_id, "CONTINUATION_CANARY", False, threading.Event())
        assert second.session_id == first.session_id
        assert runner.harness.client._proc is first_process
        assert "WIRE_CANARY" in json.dumps(endpoint["requests"][-1]["messages"])
        new_index = len(endpoint["requests"])
        independent = runner.run("session-" + uuid.uuid4().hex, "NEW_TASK", True, threading.Event())
        assert independent.session_id != first.session_id
        assert runner.harness.client._proc is first_process
        assert "WIRE_CANARY" not in json.dumps(endpoint["requests"][-1]["messages"])
        assert new_index > continue_index
        instructions = bridge("protocol").COMMON_INSTRUCTIONS
        for label, index in (
            ("fresh", 0),
            ("continue", continue_index),
            ("new session", new_index),
        ):
            request = endpoint["requests"][index]
            system_count, user_count = _instruction_counts(request, instructions)
            assert system_count == 1, f"COMMON_INSTRUCTIONS must appear once in system: {label}"
            assert user_count == 0, f"COMMON_INSTRUCTIONS must not appear in user: {label}"
    finally:
        runner.close()
    # No privacy patch. Deliberately opt in to BOTH plugins so the test proves
    # each field's mechanism, including the session-log default-off version.
    unsafe = tmp_path / "unsafe.yml"
    unsafe.write_text(
        "\n".join(
            [
                "- id: session-log-deepseek",
                "  name: '@deepseek-ai/dsh-session-log-deepseek'",
                "  config:",
                "    enabled: true",
                "- id: plugin-package-inventory-deepseek",
                "  name: '@deepseek-ai/dsh-plugin-package-inventory-deepseek'",
                "  config:",
                "    enabled: true",
                "",
            ]
        )
    )
    with DeepSeekHarness(
        dsh_home=str(tmp_path / "unsafe-home"),
        cwd=str(wire_env),
        profile="sdk-minimal",
        provider="deepseek-official",
        model="deepseek-flash",
        reasoning_effort="max",
        patches=(str(unsafe),),
        env={"DSH_TELEMETRY_MODE": "DISABLED", "DSH_TELEMETRY_DISABLED": "1"},
    ) as harness:
        harness.run("NEGATIVE_CANARY", session_id="session-" + uuid.uuid4().hex)
    negative = endpoint["requests"][-1]
    assert "dsh_plugin_packages" in negative
    assert "dsh_session_log" in negative
    assert not endpoint["otel"]
    assert KEY not in "".join(capfd.readouterr())
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()
    assert importlib.metadata.version("deepseek-harness-sdk") == "0.1.5rc1"
    assert importlib.metadata.version("deepseek-harness-runtime-bin") == "0.1.5rc1"
    print(
        "WIRE_OBSERVATION "
        + json.dumps(
            {
                "sdk": importlib.metadata.version("deepseek-harness-sdk"),
                "fields": sorted(safe),
                "model": safe["model"],
                "reasoning_effort": safe["reasoning_effort"],
                "negative_fields": sorted(negative),
                "key_body_exposed": KEY in json.dumps(safe),
                "otel_collector_requests": len(endpoint["otel"]),
                "egress_enforced": os.environ.get("BRIDGE_WIRE_SANDBOX") == "1",
            },
            sort_keys=True,
        )
    )


def test_real_shell_cwd_edit_and_test(endpoint, wire_env):
    endpoint["mode"] = "tool"
    # The fixture model asks the REAL Harness shell tool to perform bounded work.
    endpoint["command"] = (
        "pwd; printf 'VALUE = 42\\n' > example.py; "
        "printf 'import unittest\\nfrom example import VALUE\\n"
        "class Check(unittest.TestCase):\\n def test_value(self): self.assertEqual(VALUE, 42)\\n' "
        "> test_example.py; " + sys.executable + " -m unittest -v test_example"
    )
    runner = bridge("runtime").Runtime(wire_env)
    try:
        runner.run("session-" + uuid.uuid4().hex, "Run the fixture test", True, threading.Event())
    finally:
        runner.close()
    assert len(endpoint["requests"]) == 2
    tool_results = [
        message for message in endpoint["requests"][-1]["messages"] if message.get("role") == "tool"
    ]
    observed = json.dumps(tool_results)
    assert (wire_env / "example.py").read_text() == "VALUE = 42\n"
    assert str(wire_env) in observed
    assert "Ran 1 test" in observed and "OK" in observed
    assert not (wire_env / ".git").exists()


def test_wire_egress_guard():
    if os.environ.get("BRIDGE_WIRE_SANDBOX") != "1":
        pytest.skip("egress boundary unverified; use tests/wire_sandbox.py")
    with socket.socket() as probe:
        probe.settimeout(0.5)
        with pytest.raises(OSError) as denied:
            probe.connect(("192.0.2.1", 443))
        assert denied.value.errno in {errno.EPERM, errno.EACCES, errno.ENETUNREACH}


@pytest.mark.parametrize(
    "mode,category",
    [
        (401, "authentication_error"),
        (429, "transport_error"),
        (503, "transport_error"),
        ("malformed", "harness_protocol_error"),
    ],
)
async def test_real_api_errors(endpoint, wire_env, mode, category):
    endpoint["mode"] = mode
    manager = bridge("tasks").TaskManager(wire_env)
    try:
        task = await manager.start("Exercise failure classification")
        result = await wait_terminal(manager, task["task_id"], 15.0)
        assert result["status"] == "failed", result
        assert result["error"]["class"] == category, result
        assert KEY not in str(result)
        assert len(endpoint["requests"]) == 1
    finally:
        await manager.shutdown()


async def test_real_abort_runtime_recreation_and_crash(endpoint, wire_env):
    endpoint["mode"] = "hang"
    manager = bridge("tasks").TaskManager(wire_env)
    try:
        task = await manager.start("Wait until aborted")
        assert await asyncio.to_thread(endpoint["arrived"].wait, 10)
        process = manager.runtime.harness.client._proc
        assert process.poll() is None
        assert (await manager.abort(task["task_id"]))["status"] == "aborted"
        assert process.poll() is not None
        endpoint["mode"] = "ok"
        endpoint["arrived"].clear()
        fresh = await manager.start("Fresh runtime")
        fresh_result = await wait_terminal(manager, fresh["task_id"], 15.0)
        assert fresh_result["status"] == "completed"
        process2 = manager.runtime.harness.client._proc
        assert process2.pid != process.pid
        endpoint["mode"] = "hang"
        endpoint["arrived"].clear()
        crash = await manager.start("Runtime crash")
        assert await asyncio.to_thread(endpoint["arrived"].wait, 10)
        process2.kill()
        failed = await wait_terminal(manager, crash["task_id"], 15.0)
        assert failed["status"] == "failed"
        assert failed["error"]["class"] == "harness_protocol_error"
        assert process2.poll() is not None
    finally:
        await manager.shutdown()
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="Requires POSIX process inspection"
)
@pytest.mark.parametrize("shutdown", [False, True])
async def test_active_shell_reaped_on_abort_or_shutdown(endpoint, wire_env, shutdown):
    endpoint["mode"] = "slowtool"
    endpoint["command"] = "printf '%s\\n' $$ > shell.pid; sleep 120"
    manager = bridge("tasks").TaskManager(wire_env)
    task = await manager.start("Run a bounded cancellation fixture")
    try:
        marker = wire_env / "shell.pid"
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists(), "the real shell did not start"
        pid = int(marker.read_text())
        if shutdown:
            await manager.shutdown()
            expected = "interrupted"
        else:
            assert (await manager.abort(task["task_id"]))["status"] == "aborted"
            expected = "aborted"
        assert (await manager.wait(task["task_id"], 0))["status"] == expected
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
    finally:
        await manager.shutdown()


def test_git_execution_git_configuration_environment(endpoint, wire_env, monkeypatch):
    endpoint["mode"] = "tool"
    settings = (
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("core.untrackedCache", "false"),
        ("commit.gpgSign", "false"),
        ("status.submoduleSummary", "false"),
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for index, (key, value) in enumerate(settings):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("WIRE_SECRET_CANARY", "must-not-reach")
    monkeypatch.setenv("WIRE_PASSWORD_CANARY", "must-not-reach")
    monkeypatch.setenv("WIRE_TOKEN_CANARY", "must-not-reach")
    monkeypatch.setenv("WIRE_OTHER_KEY", "must-not-reach")
    checks = " && ".join(f'test "$(git config --get {key})" = "{value}"' for key, value in settings)
    endpoint["command"] = (
        "echo ENV_COUNT=$GIT_CONFIG_COUNT; "
        "echo KEY_COUNT=$(printenv | grep -c GIT_CONFIG_KEY_); "
        "echo VALUE_COUNT=$(printenv | grep -c GIT_CONFIG_VALUE_); "
        "echo SECRET_CANARY_COUNT=$(printenv | grep -ci -e WIRE_SECRET_CANARY "
        "-e WIRE_PASSWORD_CANARY -e WIRE_TOKEN_CANARY -e WIRE_OTHER_KEY "
        "-e DEEPSEEK_API_KEY); " + checks + " && echo GIT_CONFIG_GET=0 || echo GIT_CONFIG_GET=1"
    )
    runner = bridge("runtime").Runtime(wire_env)
    try:
        runner.run(
            "session-" + uuid.uuid4().hex,
            "Run the fixture Git configuration check",
            True,
            threading.Event(),
        )
    finally:
        runner.close()
    assert len(endpoint["requests"]) == 2
    tool_results = [
        message for message in endpoint["requests"][-1]["messages"] if message.get("role") == "tool"
    ]
    assert tool_results, "the fixture shell tool produced no result"
    observed = json.dumps(tool_results)
    assert "ENV_COUNT=7" in observed
    assert "KEY_COUNT=7" in observed
    assert "VALUE_COUNT=7" in observed
    assert "SECRET_CANARY_COUNT=0" in observed
    assert "GIT_CONFIG_GET=0" in observed


async def test_git_execution_pager_bypass_without_terminal_environment(
    endpoint, wire_env, monkeypatch, tmp_path
):
    checkout = Path(__file__).resolve().parents[1]
    home = tmp_path / "git-home"
    home.mkdir()
    tasks = bridge("tasks")
    # Test-only shortened deadlines; TaskManager keeps its production values.
    monkeypatch.setattr(tasks, "INACTIVITY_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(tasks, "MODEL_WAIT_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(tasks, "HARD_TIMEOUT_SECONDS", 90)
    endpoint["mode"] = "tool"
    endpoint["command"] = (
        f"env -i PATH=$PATH HOME={home} git -c core.pager=delta -C {checkout} "
        "status --porcelain; echo STATUS_EXIT=$?; "
        f"env -i PATH=$PATH HOME={home} git -c core.pager=delta -C {checkout} "
        "log --format=format:fixture -3; echo LOG_EXIT=$?; "
        "echo GIT_EXECUTION_DONE"
    )
    manager = tasks.TaskManager(wire_env)
    try:
        task = await manager.start("Run the fixture Git pager command")
        result = await wait_terminal(manager, task["task_id"], 60.0)
        assert result["status"] == "completed", result
        assert len(endpoint["requests"]) == 2
        tool_results = [
            message
            for message in endpoint["requests"][-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_results, "the fixture shell tool produced no result"
        observed = json.dumps(tool_results)
        assert "GIT_EXECUTION_DONE" in observed
        assert "STATUS_EXIT=0" in observed
        assert "LOG_EXIT=0" in observed
    finally:
        await manager.shutdown()
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())


def test_git_execution_explicit_pager_remains_available(endpoint, wire_env):
    checkout = Path(__file__).resolve().parents[1]
    endpoint["mode"] = "tool"
    endpoint["command"] = (
        f"GIT_PAGER='printf PAGER_ACTIVE; cat' git --paginate -C {checkout} "
        "log --format=format:fixture -1; printf PAGER_FINISHED"
    )
    runner = bridge("runtime").Runtime(wire_env)
    try:
        runner.run(
            "session-" + uuid.uuid4().hex, "Run the explicit pager fixture", True, threading.Event()
        )
    finally:
        runner.close()
    results = [m for m in endpoint["requests"][-1]["messages"] if m.get("role") == "tool"]
    observed = json.dumps(results)
    assert "PAGER_ACTIVE" in observed
    assert "PAGER_FINISHED" in observed


@pytest.mark.parametrize("form", ["heredoc", "python"])
async def test_execution_shell_preserves_literal_exclamation(endpoint, wire_env, monkeypatch, form):
    tasks = bridge("tasks")
    monkeypatch.setattr(tasks, "INACTIVITY_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(tasks, "HARD_TIMEOUT_SECONDS", 20)
    endpoint["mode"] = "tool"
    expected = "if (!ready) { return false; }\n"
    if form == "heredoc":
        command = "cat > literal.txt <<'EOF'\n" + expected + "EOF\n"
    else:
        import shlex

        code = "from pathlib import Path; Path('literal.txt').write_text(" + repr(expected) + ")"
        command = shlex.quote(sys.executable) + " -c " + shlex.quote(code) + "; "
    endpoint["command"] = command + "printf EXECUTION_DONE"
    manager = tasks.TaskManager(wire_env)
    try:
        accepted = await manager.start("Execute the literal-writing fixture")
        result = await wait_terminal(manager, accepted["task_id"], 15)
        assert result["status"] == "completed", result["error"]
        assert (wire_env / "literal.txt").read_text() == expected
        messages = endpoint["requests"][-1]["messages"]
        assert any(m.get("role") == "tool" and "EXECUTION_DONE" in json.dumps(m) for m in messages)
    finally:
        await manager.shutdown()
    assert not any(t.name.startswith("deepseek-worker") for t in threading.enumerate())
