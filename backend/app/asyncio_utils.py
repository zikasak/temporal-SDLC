"""Async boundaries for libraries and filesystem operations with synchronous APIs."""

import asyncio
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import ParamSpec, TypeVar

P = ParamSpec('P')
T = TypeVar('T')


async def run_sync(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Offload blocking work, finishing it before cancellation unwinds its resources.

    Cancelling to_thread does not stop its thread. Waiting for completion prevents
    temporary-directory cleanup or file closure racing a still-running operation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve a failure without replacing the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise


@asynccontextmanager
async def async_context(factory, *args, **kwargs):
    """Open and close a synchronous resource without blocking the event loop."""

    def enter():
        manager = factory(*args, **kwargs)
        return manager, manager.__enter__()

    # Keep ownership even if cancellation arrives while the resource is opening.
    opened = []

    def acquire():
        opened.append(enter())

    try:
        await run_sync(acquire)
    except BaseException:
        if opened:
            await run_sync(opened[0][0].__exit__, *sys.exc_info())
        raise
    manager, resource = opened[0]
    try:
        yield resource
    except BaseException:
        if not await run_sync(manager.__exit__, *sys.exc_info()):
            raise
    else:
        await run_sync(manager.__exit__, None, None, None)
