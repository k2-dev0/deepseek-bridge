import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest
from conftest import bridge


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
        cwd=repo,
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
            assert "top-secret" not in json.dumps(result)
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stdout.read() == b""
        assert b"mcp-fixture-key" not in await process.stderr.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_signal_exits_while_client_keeps_stdin_open(repo, signum, active, partial):
    bridge("server")
    env = {**os.environ, "DEEPSEEK_API_KEY": "signal-fixture-key"}
    report = repo / "shutdown.json"
    # Real stdio transport and TaskManager, with a controllably blocking SDK seam.
    # The audit records only terminal status after the real shutdown has joined.
    script = f"""
import json, threading
from pathlib import Path
from deepseek_harness import RunResult
from deepseek_bridge import tasks, server
class Runtime:
    def __init__(self, workspace):
        self.done = threading.Event()
    def run(self, session_id, message, fresh, stop):
        self.done.wait(10)
        return RunResult(session_id, '{{}}', 'completed', [], [])
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
        cwd=repo,
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
