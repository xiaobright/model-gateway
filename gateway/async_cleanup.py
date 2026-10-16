"""资源收尾必须完成：同时隔离 AnyIO 作用域取消和重复的 Task.cancel。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import anyio


async def finish_cleanup(operation: Awaitable[Any]) -> Any:
    """独立运行收尾，等待期间收到的直接取消在收尾结束后重新抛出。"""
    task = asyncio.ensure_future(operation)
    cancelled = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    break
                cancelled = exc
            except Exception:
                break
        try:
            return task.result()
        finally:
            if cancelled is not None:
                raise cancelled
