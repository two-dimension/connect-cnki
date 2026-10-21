"""Cancellable subprocesses; captured output never touches the MCP transport."""
from __future__ import annotations

import asyncio
import subprocess
import sys

from anyio import CancelScope

from .core import AppError


async def reap(process):
    # MCP uses level-triggered AnyIO cancellation: cleanup itself needs a shield.
    with CancelScope(shield=True):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()


async def run_process(arguments, *, timeout, input_data=None, timeout_code="OPERATION_TIMEOUT"):
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *arguments, stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            try:
                process = await spawn
            except Exception:
                pass
            else:
                await reap(process)
        raise
    except FileNotFoundError:
        raise AppError("DEPENDENCY_UNAVAILABLE", "本地处理程序不存在。", "检查依赖安装。") from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(input_data), timeout)
        return process.returncode, stdout
    except (TimeoutError, asyncio.CancelledError) as error:
        await reap(process)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise AppError(timeout_code, "本地处理超时，子进程已回收。", "检查文件或调整超时后重试。") from None
