"""How a workflow ends as cancelled on its own, such as a delivery whose pull request was
closed without being merged, and the flow sessions that end with it. Safe to import from
workflow code."""

import asyncio
from typing import NoReturn

from temporalio import workflow


async def end_cancelled(reason: str) -> NoReturn:
    """End the current workflow run with execution status CANCELED.

    A workflow run ends as cancelled when it raises a cancellation after a cancellation
    was requested of it. The run requests its own cancellation through the server, as a
    request to cancel an external workflow naming its own ID and run, and waits for it:
    the server delivers it to the run as any cancellation request, and the cancellation
    raised then ends the run as CANCELED. Both are recorded in the run's history, so a
    replay ends the same way."""
    info = workflow.info()
    try:
        await workflow.get_external_workflow_handle(
            info.workflow_id, run_id=info.run_id).cancel()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back to a cancellation of the run
        workflow.logger.warning('Could not request the cancellation of this run: %s', exc)
        runtime = workflow._Runtime.current()  # noqa: SLF001 - no public API for this
        if getattr(runtime, '_cancel_reason', None) is None:
            setattr(runtime, '_cancel_reason', reason)
        raise asyncio.CancelledError(reason) from exc
    # The requested cancellation arrives as a cancellation of this wait.
    await workflow.wait_condition(lambda: False)
    raise asyncio.CancelledError(reason)
