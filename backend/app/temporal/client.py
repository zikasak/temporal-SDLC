import asyncio
import logging
import uuid

from temporalio.client import Client

from app.config import TASK_QUEUE, TEMPORAL_ADDRESS
from app.logging_setup import configure_logging

logger = logging.getLogger(__name__)


class TemporalUnavailable(Exception):
    """Temporal could not be reached or failed to answer."""


async def connect_temporal() -> Client:
    try:
        return await Client.connect(TEMPORAL_ADDRESS)
    except Exception as exc:
        logger.error("Could not connect to Temporal at %s: %s", TEMPORAL_ADDRESS, exc)
        raise TemporalUnavailable("Temporal is unavailable") from exc


async def start_workflow() -> str:
    client = await connect_temporal()
    try:
        return await client.execute_workflow(
            "SayHelloWorkflow",
            "Temporal2",
            id=f"say-hello-workflow-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    except TemporalUnavailable:
        raise
    except Exception as exc:
        logger.error("Sample workflow failed: %s", exc)
        raise TemporalUnavailable("Temporal could not run the sample workflow") from exc


async def main():
    configure_logging()
    result = await start_workflow()
    logger.info("Workflow result: %s", result)

if __name__ == "__main__":
    asyncio.run(main())
