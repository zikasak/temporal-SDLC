from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.common import SearchAttributeKey
from temporalio.testing import WorkflowEnvironment

from helpers import RECORD, Recorder

# The dev server reads each history shard's task queues at most 20 times a second by
# default, so every workflow task, activity or child started right after another waits up
# to 50 ms; without the limit the tests run in a fraction of the time.
FAST_TASK_DISPATCH = [
    arg for setting in ('history.transferProcessorMaxPollRPS=10000',
                        'history.timerProcessorMaxPollRPS=10000',
                        'history.visibilityProcessorMaxPollRPS=10000')
    for arg in ('--dynamic-config-value', setting)]


@pytest.fixture(autouse=True)
def recorder() -> Recorder:
    """What the fake workflows saw, empty at the start of each test."""
    RECORD.clear()
    return RECORD


@pytest_asyncio.fixture
async def skipping_env() -> AsyncIterator[WorkflowEnvironment]:
    """An in-process Temporal test server whose timers do not take real time."""
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture
async def local_env() -> AsyncIterator[WorkflowEnvironment]:
    """A local Temporal dev server (it takes updates and lists executions)."""
    env = await WorkflowEnvironment.start_local(
        search_attributes=[
            SearchAttributeKey.for_keyword("Repository"),
            SearchAttributeKey.for_keyword("Branch"),
            SearchAttributeKey.for_keyword("OnboardingId"),
        ],
        dev_server_extra_args=FAST_TASK_DISPATCH,
    )
    try:
        yield env
    finally:
        await env.shutdown()
