import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from app.config import CODEGEN_TASK_QUEUE, TEMPORAL_ADDRESS
from app.logging_setup import configure_logging
from app.temporal.lifecycle import run_worker

with workflow.unsafe.imports_passed_through():
    from app.codegen.activities import generate_server_code
    from app.codegen.workflows import CodegenWorkflow


logger = logging.getLogger(__name__)


async def main():
    configure_logging()
    client = await Client.connect(TEMPORAL_ADDRESS)
    worker = Worker(
        client,
        task_queue=CODEGEN_TASK_QUEUE,
        workflows=[CodegenWorkflow],
        activities=[generate_server_code],
    )
    logger.info("Codegen worker started on task queue %s at %s",
                CODEGEN_TASK_QUEUE, TEMPORAL_ADDRESS)
    await run_worker(worker)

if __name__ == "__main__":
    asyncio.run(main())
