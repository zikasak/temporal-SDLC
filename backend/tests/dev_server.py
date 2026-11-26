"""The local Temporal dev server the tests run workflows on."""
from typing import Any

from temporalio.testing import WorkflowEnvironment

# The dev server reads each history shard's task queues at most 20 times a second by
# default, so a workflow task, an activity or a child started right after another waits up
# to 50 ms. A delivery takes hundreds of such steps: without the limit its tests run in a
# fraction of the time.
# The server also holds back a run continued as new, or a workflow started again under
# the same ID, until a second after the previous run started; a retry or restart of a
# stage would otherwise wait out that second.
FAST_TASK_DISPATCH = [
    arg for setting in ('history.transferProcessorMaxPollRPS=10000',
                        'history.timerProcessorMaxPollRPS=10000',
                        'history.visibilityProcessorMaxPollRPS=10000',
                        'history.workflowIdReuseMinimalInterval="0s"')
    for arg in ('--dynamic-config-value', setting)]


async def start_dev_server(**kwargs: Any) -> WorkflowEnvironment:
    """WorkflowEnvironment.start_local, with tasks dispatched as soon as they are due."""
    extra = [*(kwargs.pop('dev_server_extra_args', None) or []), *FAST_TASK_DISPATCH]
    return await WorkflowEnvironment.start_local(dev_server_extra_args=extra, **kwargs)
