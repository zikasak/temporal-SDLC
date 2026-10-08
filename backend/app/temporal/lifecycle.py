"""Run a Temporal worker as a container's main process."""
import asyncio
import signal

from temporalio.worker import Worker

from app.tracing import shutdown_tracing


async def run_worker(worker: Worker) -> None:
    """Run until SIGTERM or SIGINT, then shut the worker down and flush tracing.

    A container's main process ignores SIGTERM unless it installs a handler: without one,
    `docker stop` waits out its whole timeout and then kills the worker.
    """
    loop = asyncio.get_running_loop()
    run = asyncio.ensure_future(worker.run())
    for signum in (signal.SIGTERM, signal.SIGINT):
        # Cancelling a running worker makes it shut down.
        loop.add_signal_handler(signum, run.cancel)
    try:
        await run
    except asyncio.CancelledError:
        if not run.cancelled():
            raise
    finally:
        await asyncio.to_thread(shutdown_tracing)
