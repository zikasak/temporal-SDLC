"""Host-only bootstrap for a dedicated single-stage Temporal worker."""
import logging
from collections.abc import Callable
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import TEMPORAL_ADDRESS
from app.logging_setup import configure_logging
from app.temporal.lifecycle import run_worker


async def run_agent_worker(*, task_queue: str, workflow_type: type,
                           activities: list[Callable[..., Any]], label: str) -> None:
    configure_logging()
    client = await Client.connect(TEMPORAL_ADDRESS)
    worker = Worker(client, task_queue=task_queue, workflows=[workflow_type],
                    activities=activities, max_concurrent_activities=1)
    logging.getLogger(__name__).info('%s worker started on %s', label, task_queue)
    await run_worker(worker)
