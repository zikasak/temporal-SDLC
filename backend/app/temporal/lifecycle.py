"""Run Temporal workers as a container's main process."""
import asyncio
import signal
from typing import Any

from temporalio.worker import Worker

from app.asyncio_utils import run_sync
from app.tracing import shutdown_tracing


async def run_worker(*workers: Worker) -> None:
    """Run until SIGTERM or SIGINT, then shut the workers down and flush tracing.

    A container's main process ignores SIGTERM unless it installs a handler: without one,
    `docker stop` waits out its whole timeout and then kills the worker. The workers run
    together: when one stops or fails, the others are shut down and a failure is raised.
    """
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    runs: list[asyncio.Future[Any]] = [asyncio.ensure_future(worker.run()) for worker in workers]
    stop: asyncio.Future[Any] = asyncio.ensure_future(stopped.wait())
    try:
        await asyncio.wait([*runs, stop], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        # Cancelling a running worker makes it shut down.
        for task in (*runs, stop):
            task.cancel()
        results = await asyncio.gather(*runs, stop, return_exceptions=True)
        await run_sync(shutdown_tracing)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result
