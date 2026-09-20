import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from conftest import bridge


def server_cwd(repo):
    if (repo / ".git").exists():
        return repo
    # Sandboxes that deny creating .git still expose the checked-out worktree;
    # read-only tools/list and rejected inputs never write into it.
    fallback = Path(__file__).resolve().parents[1]
    if not (fallback / ".git").exists():
        pytest.skip("no git worktree is available for the MCP server")
    return fallback


@pytest.mark.parametrize("entrypoint", ["module", "console"])
async def test_stdio_tools_and_invalid_inputs(repo, tmp_path, entrypoint):
    bridge("server")
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "DEEPSEEK_API_KEY": "mcp-fixture-key",
    }
    command = (
        [sys.executable, "-m", "deepseek_bridge"]
        if entrypoint == "module"
        else [str(Path(sys.executable).parent / "deepseek-bridge")]
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=server_cwd(repo),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    async def receive():
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 10))

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            }
        )
        assert (await receive())["id"] == 1
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = (await receive())["result"]["tools"]
        assert {tool["name"] for tool in listed} == {
            "start_task",
            "wait_task",
            "continue_task",
            "abort_task",
        }
        for tool in listed:
            schema = tool["inputSchema"]
            assert schema["additionalProperties"] is False
            assert not {"workspace", "model", "provider", "profile"} & schema["properties"].keys()
        wait_tool = next(tool for tool in listed if tool["name"] == "wait_task")
        assert set(wait_tool["inputSchema"]["properties"]) == {"task_id"}
        output_schema = wait_tool.get("outputSchema")
        assert output_schema, "wait_task must publish the new snapshot output schema"
        properties = output_schema["properties"]
        for field in (
            "task_id",
            "session_id",
            "status",
            "final_response",
            "finish_reason",
            "error",
            "started_at",
            "last_activity_at",
            "elapsed_ms",
            "phase",
            "observability",
        ):
            assert field in properties, f"wait_task output schema is missing {field}"
        assert "progress" not in properties
        required = set(output_schema.get("required", []))
        assert {
            "status",
            "started_at",
            "last_activity_at",
            "elapsed_ms",
            "phase",
            "observability",
        } <= required
        assert properties["observability"].get("enum") == ["available", "unavailable"]
        assert {
            "starting",
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
            "completed",
            "needs_decision",
            "failed",
            "aborted",
            "interrupted",
        } <= set(properties["phase"].get("enum", []))
        for index, arguments in enumerate(
            (
                {"brief": " "},
                {"brief": "ok", "workspace": "/"},
                {"brief": "DEEPSEEK_API_KEY=top-secret"},
            ),
            3,
        ):
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": "start_task", "arguments": arguments},
                }
            )
            result = await receive()
            assert result.get("error") or result["result"]["isError"]
            assert result["result"]["isError"] is True
            content = result["result"]["content"]
            assert len(content) == 1
            assert content[0]["type"] == "text"
            # This SDK passes arguments through to the handler: the extra key
            # is an INPUTS model failure, not an SDK-layer schema rejection.
            assert json.loads(content[0]["text"]) == {
                "class": "configuration_error",
                "message": "Invalid configuration, input, task ID, or task state.",
                "rejection": "input_validation",
                "execution_started": False,
            }
            assert "top-secret" not in json.dumps(result)
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stdout.read() == b""
        assert b"mcp-fixture-key" not in await process.stderr.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_input_rejection_never_starts_manager(repo, tmp_path):
    bridge("server")
    canary = "red-canary-4f19"
    credential = "api" + "_key=" + canary
    audit = tmp_path / "start-calls.json"
    # Real stdio transport and server; only TaskManager is replaced so the
    # audit sees whether input rejection ever reaches manager.start.
    script = f"""
import json
from pathlib import Path
from deepseek_bridge import server
from deepseek_bridge.protocol import BridgeError, StartInput

audit = Path({str(audit)!r})
audit.write_text("[]")
calls = []

class AuditManager:
    def __init__(self, workspace):
        pass

    def _record(self, tool, **values):
        calls.append({{"tool": tool, **values}})
        audit.write_text(json.dumps(calls))

    async def start(self, brief, title=None):
        self._record("start", brief=brief, title=title)
        if brief == "audit-bridge-error":
            raise BridgeError("configuration_error")
        if brief == "audit-internal-validation":
            StartInput.model_validate({{"brief": " "}})
        return {{
            "task_id": f"task-audit-{{len(calls)}}",
            "session_id": "session-audit",
            "status": "running",
        }}

    async def wait(self, task_id):
        self._record("wait", task_id=task_id)
        return {{
            "task_id": task_id,
            "session_id": "session-audit",
            "status": "completed",
            "phase": "completed",
            "observability": "available",
        }}

    async def continue_task(self, task_id, message):
        self._record("continue_task", task_id=task_id)
        return {{"task_id": task_id, "session_id": "session-audit", "status": "running"}}

    async def abort(self, task_id):
        self._record("abort", task_id=task_id)
        return {{"task_id": task_id, "session_id": "session-audit", "status": "aborted"}}

    async def shutdown(self):
        pass

server.TaskManager = AuditManager
server.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        cwd=server_cwd(repo),
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "DEEPSEEK_API_KEY": "mcp-fixture-key",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    async def receive():
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 10))

    async def call(request_id, name, arguments):
        await send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return await receive()

    rejection = {
        "class": "configuration_error",
        "message": "Invalid configuration, input, task ID, or task state.",
        "rejection": "input_validation",
        "execution_started": False,
    }
    old_error = {
        "class": "configuration_error",
        "message": "Invalid configuration, input, task ID, or task state.",
    }
    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "audit-fixture", "version": "1"},
                },
            }
        )
        assert (await receive())["id"] == 1
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        for index, arguments in enumerate(
            (
                {"brief": " "},
                {"brief": credential},
                {"brief": "audit-title-brief", "title": " "},
            ),
            2,
        ):
            result = await call(index, "start_task", arguments)
            assert result["result"]["isError"] is True
            content = result["result"]["content"]
            assert len(content) == 1
            assert content[0]["type"] == "text"
            parsed = json.loads(content[0]["text"])
            assert parsed == rejection
            assert parsed["execution_started"] is False
            assert "task_id" not in json.dumps(result)
            assert canary not in json.dumps(result)
            assert credential not in json.dumps(result)
            assert json.loads(audit.read_text()) == []

        accepted = await call(5, "start_task", {"brief": "audit-normal-brief"})
        assert accepted["result"]["isError"] is False
        assert accepted["result"]["structuredContent"]["task_id"] == "task-audit-1"
        assert json.loads(audit.read_text()) == [
            {"tool": "start", "brief": "audit-normal-brief", "title": None}
        ]

        for index, brief in enumerate(("audit-bridge-error", "audit-internal-validation"), 6):
            result = await call(index, "start_task", {"brief": brief})
            assert result["result"]["isError"] is True
            content = result["result"]["content"]
            assert len(content) == 1
            assert content[0]["type"] == "text"
            assert json.loads(content[0]["text"]) == old_error
            assert "rejection" not in json.dumps(result)
            assert "execution_started" not in json.dumps(result)
        # Both starts were called; only the input boundary gets the marker.
        assert [entry["brief"] for entry in json.loads(audit.read_text())] == [
            "audit-normal-brief",
            "audit-bridge-error",
            "audit-internal-validation",
        ]

        for index, (name, arguments) in enumerate(
            (
                ("continue_task", {"task_id": "task-audit-1", "message": " "}),
                ("unknown_tool", {}),
            ),
            8,
        ):
            result = await call(index, name, arguments)
            assert result["result"]["isError"] is True
            assert json.loads(result["result"]["content"][0]["text"]) == old_error
            assert "rejection" not in json.dumps(result)
            assert "execution_started" not in json.dumps(result)

        # The SDK rejects a wrong-typed arguments member before the handler, so
        # that old JSON-RPC error cannot serve as the new non-start proof.
        sdk_rejected = await call(10, "start_task", "not-an-object")
        assert sdk_rejected["error"]["code"] == -32602
        assert "result" not in sdk_rejected
        assert "rejection" not in json.dumps(sdk_rejected)
        assert "execution_started" not in json.dumps(sdk_rejected)
        assert len(json.loads(audit.read_text())) == 3

        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stdout.read() == b""
        stderr = await process.stderr.read()
        assert b"mcp-fixture-key" not in stderr
        assert canary.encode() not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_signal_exits_while_client_keeps_stdin_open(repo, tmp_path, signum, active, partial):
    bridge("server")
    env = {**os.environ, "DEEPSEEK_API_KEY": "signal-fixture-key"}
    report = tmp_path / "shutdown.json"
    # Real stdio transport and TaskManager, with a controllably blocking SDK seam.
    # The audit records only terminal status after the real shutdown has joined.
    script = f"""
import json, threading
from pathlib import Path
from deepseek_harness import RunResult
from deepseek_bridge import tasks, server
class Runtime:
    def __init__(self, workspace, *args, **kwargs):
        self.done = threading.Event()
    def run(self, session_id, message, fresh, stop, *args, **kwargs):
        self.done.wait(10)
        return RunResult(session_id, '{{}}', 'completed', [], [])
    def owned_process(self):
        return None
    def force_stop(self):
        return True
    def close(self):
        self.done.set()
tasks.Runtime = Runtime
shutdown = tasks.TaskManager.shutdown
async def audit(self):
    await shutdown(self)
    Path({str(report)!r}).write_text(json.dumps([t.status for t in self._tasks.values()]))
tasks.TaskManager.shutdown = audit
server.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        cwd=server_cwd(repo),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "signal-fixture", "version": "1"},
            },
        }
        process.stdin.write((json.dumps(initialize) + "\n").encode())
        await process.stdin.drain()
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert reply["id"] == 1
        if active:
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "start_task", "arguments": {"brief": "Work"}},
                    }
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
            started = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert started["result"]["structuredContent"]["status"] == "running"
        if partial:
            process.stdin.write(b'{"jsonrpc": ')
            await process.stdin.drain()
        process.send_signal(signum)
        assert await asyncio.wait_for(process.wait(), 2) == 0
        assert json.loads(report.read_text()) == (["interrupted"] if active else [])
        assert b"signal-fixture-key" not in await process.stderr.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        process.stdin.close()


@pytest.mark.parametrize("exit_trigger", ["eof", "sigterm"])
@pytest.mark.parametrize("stuck", ["close", "worker", "force_stop", "error_close"])
async def test_cli_exits_with_unreleased_cleanup_threads(repo, exit_trigger, stuck):
    # Keep the fault in place through interpreter exit. Releasing the threads in
    # the child would hide asyncio.run()/ThreadPoolExecutor's implicit joins.
    script = f"""
import threading
from deepseek_harness import RunResult
from deepseek_bridge import tasks, server
tasks.HARD_TIMEOUT_SECONDS = 0.1
tasks.INACTIVITY_TIMEOUT_SECONDS = 60
tasks.CLEANUP_GRACE_SECONDS = 0.05
tasks.CLEANUP_JOIN_SECONDS = 0.05
tasks.CLEANUP_FORCE_SECONDS = 0.05
class Runtime:
    def __init__(self, workspace):
        self.done = threading.Event()
    def run(self, session_id, message, fresh, stop, activity):
        if {stuck!r} == 'error_close':
            raise RuntimeError('cli-private-error-canary')
        self.done.wait()
        return RunResult(session_id, '{{}}', 'completed', [], [])
    def owned_process(self):
        return None
    def close(self):
        if {stuck!r} != 'worker':
            threading.Event().wait()
    def force_stop(self):
        if {stuck!r} == 'force_stop':
            threading.Event().wait()
        self.done.set()
        return True
tasks.Runtime = Runtime
server.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        cwd=server_cwd(repo),
        env={**os.environ, "DEEPSEEK_API_KEY": "cli-cleanup-fixture-key"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def request(request_id, method, params):
        process.stdin.write(
            (
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                + "\n"
            ).encode()
        )
        await process.stdin.drain()
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 3))

    try:
        await request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cleanup-fixture", "version": "1"},
            },
        )
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        started = await request(
            2,
            "tools/call",
            {
                "name": "start_task",
                "arguments": {"brief": "Exercise bounded CLI shutdown"},
            },
        )
        task_id = started["result"]["structuredContent"]["task_id"]
        terminal = await request(
            3,
            "tools/call",
            {
                "name": "wait_task",
                "arguments": {"task_id": task_id},
            },
        )
        snapshot = terminal["result"]["structuredContent"]
        assert snapshot["status"] == "failed"
        assert snapshot["error"]["class"] == "abort_error"
        if exit_trigger == "eof":
            process.stdin.close()
        else:
            process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 3) == 1
        stderr = await process.stderr.read()
        assert b"abort_error" in stderr
        assert b"cli-private-error-canary" not in stderr
        assert b"cli-cleanup-fixture-key" not in stderr
        assert await process.stdout.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        process.stdin.close()
