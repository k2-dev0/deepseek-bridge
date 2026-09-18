"""Only four stdio MCP tools. stdout is owned by the official MCP SDK."""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

from anyio import AsyncFile, CancelScope
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from . import __version__
from .protocol import (
    AbortInput,
    BridgeError,
    ContinueInput,
    StartInput,
    WaitInput,
    wait_output_schema,
)
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
    "wait_task": (
        "Wait on task status or new SDK activity for 0..60000 ms; "
        "a timeout leaves the task running."
    ),
    "continue_task": (
        "Continue a completed or needs_decision task in the same Harness session. "
        "Pass only new information or a diff; do not repeat the initial brief or "
        "confirmed requirements. Explicit corrections or additions are forwarded "
        "as-is; the bridge never summarizes or deletes message text."
    ),
    "abort_task": "Stop the active task and reclaim its runtime; preserve worktree changes.",
}


class _PipeInput(AsyncFile[str]):
    """A cancellable pipe reader for the official SDK's stdin stream argument."""

    def __init__(self, reader: asyncio.StreamReader):
        super().__init__(sys.stdin)
        self.reader = reader

    async def readline(self) -> str:
        return (await self.reader.readline()).decode("utf-8", errors="replace")


async def serve() -> None:
    workspace = bind_workspace()
    validate_environment()
    manager = TaskManager(workspace)

    async def list_tools(context: Any, params: Any) -> types.ListToolsResult:
        tools = []
        for name, model in INPUTS.items():
            tool = types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                input_schema=model.model_json_schema(),
            )
            if name == "wait_task":
                tool.output_schema = wait_output_schema()
            tools.append(tool)
        return types.ListToolsResult(tools=tools)

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
    transport: asyncio.ReadTransport | None = None
    try:
        # AsyncFile's default readline uses a non-abandonable worker thread.
        # A live client pipe would hold stdio_server's task group open forever
        # on signal shutdown. Keep input on the event loop so cancellation can
        # release that group, including a partially received JSON line.
        reader = asyncio.StreamReader(limit=1024 * 1024)
        pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        try:
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), pipe
            )
        except BaseException:
            pipe.close()
            raise
        with CancelScope() as scope:
            async with stdio_server(stdin=_PipeInput(reader)) as (read, write):
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
                    scope.cancel()
    finally:
        if transport is not None:
            transport.close()
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
