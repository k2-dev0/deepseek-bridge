"""Only four stdio MCP tools. stdout is owned by the official MCP SDK."""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from . import __version__
from .protocol import AbortInput, BridgeError, ContinueInput, StartInput, WaitInput
from .runtime import bind_workspace, validate_environment
from .tasks import TaskManager

INPUTS: dict[str, type[StartInput] | type[WaitInput] | type[ContinueInput] | type[AbortInput]] = {
    "start_task": StartInput,
    "wait_task": WaitInput,
    "continue_task": ContinueInput,
    "abort_task": AbortInput,
}
DESCRIPTIONS = {
    "start_task": "Start one background task in the bound repository (brief <= 32000 characters).",
    "wait_task": "Wait on task state for 0..60000 ms; a timeout leaves the task running.",
    "continue_task": "Continue a completed or needs_decision task in the same Harness session.",
    "abort_task": "Stop the active task and reclaim its runtime; preserve worktree changes.",
}


async def serve() -> None:
    workspace = bind_workspace()
    validate_environment()
    manager = TaskManager(workspace)

    async def list_tools(context: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=DESCRIPTIONS[name],
                    input_schema=model.model_json_schema(),
                )
                for name, model in INPUTS.items()
            ]
        )

    async def call_tool(context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            if params.name not in INPUTS:
                raise BridgeError("configuration_error")
            value = INPUTS[params.name].model_validate(params.arguments or {})
            if isinstance(value, StartInput):
                result = await manager.start(value.brief, value.title)
            elif isinstance(value, WaitInput):
                result = await manager.wait(value.task_id, value.timeout_ms)
            elif isinstance(value, ContinueInput):
                result = await manager.continue_task(value.task_id, value.message)
            else:
                result = await manager.abort(value.task_id)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structured_content=result,
            )
        except (BridgeError, ValidationError) as error:
            safe = error if isinstance(error, BridgeError) else BridgeError("configuration_error")
        except Exception:
            safe = BridgeError("internal_error")
        return types.CallToolResult(
            is_error=True,
            content=[types.TextContent(type="text", text=json.dumps(safe.as_dict()))],
        )

    server: Server[Any] = Server(
        "deepseek-bridge", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        except NotImplementedError:
            pass
    try:
        async with stdio_server() as (read, write):
            connection = asyncio.create_task(
                server.run(read, write, server.create_initialization_options())
            )
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {connection, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if connection in done:
                    await connection
            finally:
                connection.cancel()
                stopping.cancel()
                await asyncio.gather(connection, stopping, return_exceptions=True)
    finally:
        await manager.shutdown()
        for signum in installed:
            loop.remove_signal_handler(signum)


def main() -> None:
    os.umask(0o077)
    # Third-party logging may include untrusted SDK/MCP input. The bridge emits
    # only static, sanitized failures; runtime stderr stays inside SDK pipes.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve())
    except BridgeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(str(BridgeError("internal_error")), file=sys.stderr)
        raise SystemExit(1) from None
