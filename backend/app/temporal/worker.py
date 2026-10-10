import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from app.config import TASK_QUEUE, TEMPORAL_ADDRESS
from app.logging_setup import configure_logging
from app.temporal.lifecycle import run_worker

with workflow.unsafe.imports_passed_through():
    from app.delivery.workflows import DeliveryWorkflow
    from app.documents import ensure_document_schema
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        greet,
        parse_document,
        publish_repository,
        requirements_turn,
        send_llm_message,
        write_openapi_document,
        write_requirements_document,
    )
    from app.temporal.workflows import (
        ApiDesignWorkflow,
        LLMRequestWorkflow,
        RequirementsWorkflow,
        SayHelloWorkflow,
    )


logger = logging.getLogger(__name__)


async def main():
    configure_logging()
    await ensure_document_schema()
    client = await Client.connect(TEMPORAL_ADDRESS)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            SayHelloWorkflow, LLMRequestWorkflow, RequirementsWorkflow, ApiDesignWorkflow,
            DeliveryWorkflow,
        ],
        activities=[
            greet, parse_document, send_llm_message,
            requirements_turn, write_requirements_document,
            api_design_turn, write_openapi_document,
            checkout_repository, publish_repository,
        ],
    )
    logger.info("Worker started on task queue %s at %s", TASK_QUEUE, TEMPORAL_ADDRESS)
    await run_worker(worker)

if __name__ == "__main__":
    asyncio.run(main())
