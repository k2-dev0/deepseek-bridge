import asyncio
import json
import os
import sys
from pathlib import Path

from conftest import bridge


async def test_stdio_tools_and_invalid_inputs(repo, tmp_path):
    bridge("server")
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "DEEPSEEK_API_KEY": "mcp-fixture-key",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "deepseek_bridge",
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
