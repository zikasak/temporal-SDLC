import asyncio
import logging
import uuid

from temporalio.client import Client

from app.config import TASK_QUEUE, TEMPORAL_ADDRESS
from app.logging_setup import configure_logging

logger = logging.getLogger(__name__)


async def connect_temporal() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS)


async def start_workflow() -> str:
    client = await connect_temporal()
    return await client.execute_workflow(
        "SayHelloWorkflow",
        "Temporal2",
        id=f"say-hello-workflow-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )


async def main():
    configure_logging()
    result = await start_workflow()
    logger.info("Workflow result: %s", result)

if __name__ == "__main__":
    asyncio.run(main())
