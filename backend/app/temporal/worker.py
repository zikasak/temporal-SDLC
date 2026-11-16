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
    from app.architecture.activities import architecture_turn, write_architecture_document
    from app.architecture.workflows import ArchitectureWorkflow
    from app.delivery.ci_activities import (
        ci_failure_report, comment_pull_request, commit_checks,
    )
    from app.delivery.mention_activities import (
        answer_mention, classify_mention, mention_body, record_new_mention, reply_to_mention,
        save_mention,
    )
    from app.delivery.mentions import ensure_schema as ensure_mention_schema
    from app.delivery.pull_request_activities import write_pull_request_text
    from app.delivery.workflows import DeliveryWorkflow
    from app.documents import ensure_document_schema
    from app.onboarding.activities import (
        find_onboarding_artifacts, inspect_onboarding_contract, onboarding_turn,
        revise_onboarding_architecture,
    )
    from app.onboarding.workflows import OnboardingWorkflow
    from app.settings.storage import ensure_schema as ensure_settings_schema
    from app.task_flow.activities import closed_workflows, name_task_flow
    from app.task_flow.workflows import TaskWorkflow
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        open_pull_request,
        parse_document,
        publish_documentation,
        publish_onboarding,
        publish_onboarding_architecture,
        publish_repository,
        pull_request_state,
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
            LLMRequestWorkflow, TaskWorkflow, RequirementsWorkflow, ArchitectureWorkflow,
            ApiDesignWorkflow, DeliveryWorkflow, OnboardingWorkflow,
        ],
        activities=[
            parse_document, send_llm_message,
            requirements_turn, write_requirements_document,
            architecture_turn, write_architecture_document,
            api_design_turn, write_openapi_document,
            checkout_repository, update_repository_checkout, publish_repository,
            publish_documentation, publish_onboarding, publish_onboarding_architecture,
            open_pull_request, write_pull_request_text,
            read_repository_contract, read_requirements_document, register_session,
            find_onboarding_artifacts, inspect_onboarding_contract, onboarding_turn,
            revise_onboarding_architecture,
            classify_mention, answer_mention, reply_to_mention,
            record_new_mention, save_mention, mention_body,
            pull_request_state, commit_checks, ci_failure_report, comment_pull_request,
            name_task_flow, closed_workflows,
        ],
    )
    logger.info("Worker started on task queue %s at %s", TASK_QUEUE, TEMPORAL_ADDRESS)
    checks = asyncio.create_task(_check_waiting_deliveries())
    try:
        await run_worker(worker)
    finally:
        checks.cancel()


# How long after starting the worker asks the waiting deliveries to check their pull
# request, so that it polls their workflow tasks by then.
CHECK_DELAY_SECONDS = 15


async def _check_waiting_deliveries() -> None:
    """Have every delivery that waits check its pull request once the worker runs, so that
    one merged or closed while no webhook reached the instance, or before deliveries
    watched their pull request, closes with its flow."""
    from app.delivery.service import check_waiting_pull_requests
    await asyncio.sleep(CHECK_DELAY_SECONDS)
    try:
        asked = await check_waiting_pull_requests()
    except Exception:  # noqa: BLE001 - the deliveries' own checks still run
        logger.warning("Could not ask the waiting deliveries to check their pull request",
                       exc_info=True)
        return
    logger.info("Asked %d running deliveries to check their pull request", asked)

if __name__ == "__main__":
    asyncio.run(main())
