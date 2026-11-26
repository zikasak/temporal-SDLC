"""Starting the local Temporal dev server the tests share (the temporal_server fixture)."""
import uuid
from typing import Any

from google.protobuf.duration_pb2 import Duration
from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
from temporalio.client import Client
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


async def own_namespace(env: WorkflowEnvironment) -> Client:
    """A client of a new namespace on `env`'s server, with the repository's search
    attributes registered: for tests that list executions, which would otherwise see
    those of every other test sharing the server."""
    from app.temporal.search_attributes import ensure_search_attributes

    namespace = f'test-{uuid.uuid4().hex}'
    await env.client.workflow_service.register_namespace(RegisterNamespaceRequest(
        namespace=namespace, workflow_execution_retention_period=Duration(seconds=86400)))
    client = await Client.connect(env.client.service_client.config.target_host,
                                  namespace=namespace)
    await ensure_search_attributes(client)
    return client
