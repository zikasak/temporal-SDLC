import asyncio
import logging

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from app.config import TASK_QUEUE, TEMPORAL_ADDRESS
from app.logging_setup import configure_logging
from app.temporal.executions import prepare_namespace
from app.temporal.lifecycle import run_worker

with workflow.unsafe.imports_passed_through():
    from app.delivery.mention_activities import (
        answer_mention, classify_mention, mention_body, record_new_mention, reply_to_mention,
        save_mention,
    )
    from app.delivery.mentions import ensure_schema as ensure_mention_schema
    from app.delivery.pull_request_activities import write_pull_request_text
    from app.delivery.workflows import DeliveryWorkflow
    from app.documents import ensure_document_schema
    from app.onboarding.activities import find_onboarding_artifacts, onboarding_turn
    from app.onboarding.workflows import OnboardingWorkflow
    from app.settings.storage import ensure_schema as ensure_settings_schema
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        open_pull_request,
        parse_document,
        publish_documentation,
        publish_onboarding,
        publish_repository,
        read_repository_contract,
        read_requirements_document,
        register_session,
        requirements_turn,
        send_llm_message,
        update_repository_checkout,
        write_openapi_document,
        write_requirements_document,
    )
    from app.temporal.workflows import (
        ApiDesignWorkflow,
        LLMRequestWorkflow,
        RequirementsWorkflow,
    )


logger = logging.getLogger(__name__)


async def main():
    configure_logging()
    await ensure_document_schema()
    await ensure_settings_schema()
    await ensure_mention_schema()
    client = await Client.connect(TEMPORAL_ADDRESS)
    await prepare_namespace(client)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            LLMRequestWorkflow, RequirementsWorkflow, ApiDesignWorkflow,
            DeliveryWorkflow, OnboardingWorkflow,
        ],
        activities=[
            parse_document, send_llm_message,
            requirements_turn, write_requirements_document,
            api_design_turn, write_openapi_document,
            checkout_repository, update_repository_checkout, publish_repository,
            publish_documentation, publish_onboarding, open_pull_request, write_pull_request_text,
            read_repository_contract, read_requirements_document, register_session,
            find_onboarding_artifacts, onboarding_turn,
            classify_mention, answer_mention, reply_to_mention,
            record_new_mention, save_mention, mention_body,
        ],
    )
    logger.info("Worker started on task queue %s at %s", TASK_QUEUE, TEMPORAL_ADDRESS)
    await run_worker(worker)

if __name__ == "__main__":
    asyncio.run(main())
