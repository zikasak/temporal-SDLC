"""Host-only Temporal worker for every sandboxed stage, in one process.

Each stage keeps its own task queue and a Worker that runs one activity at a time, so
stages neither wait on one another nor run two containers of the same stage at once.
Run with `python -m app.agent_workflows.worker`.
"""
import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import (
    COMPONENT_TEST_GENERATION_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE, DOCUMENTATION_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE,
    REVIEW_TASK_QUEUE, ROUTING_TASK_QUEUE, TEMPORAL_ADDRESS, TEST_GENERATION_TASK_QUEUE,
    VERIFICATION_TASK_QUEUE,
)
from app.diagnosis.activities import diagnose_failure
from app.diagnosis.workflows import DiagnosisWorkflow
from app.documentation.activities import write_documentation
from app.documentation.workflows import DocumentationWorkflow
from app.implementation.activities import implement_service
from app.implementation.workflows import ImplementationWorkflow
from app.logging_setup import configure_logging
from app.review.activities import review_changes
from app.review.workflows import ReviewWorkflow
from app.routing.activities import route_requirements
from app.routing.workflows import ObservabilityWorkflow
from app.settings.storage import ensure_schema as ensure_settings_schema
from app.temporal.lifecycle import run_worker
from app.test_generation.activities import generate_service_tests
from app.test_generation.components import generate_component_tests
from app.test_generation.workflows import ComponentTestGenerationWorkflow, TestGenerationWorkflow
from app.verification.activities import (
    build_baseline, build_component_runner, build_repository_tests, run_component_suite,
    snapshot_repository_tests, verify_documentation, verify_service,
)
from app.verification.workflows import VerificationWorkflow

logger = logging.getLogger(__name__)

# (label, task queue, workflow, activities) of each sandboxed stage.
STAGES = (
    ('Routing', ROUTING_TASK_QUEUE, ObservabilityWorkflow, [route_requirements]),
    ('Implementation', IMPLEMENTATION_TASK_QUEUE, ImplementationWorkflow, [implement_service]),
    ('Test generation', TEST_GENERATION_TASK_QUEUE, TestGenerationWorkflow,
     [generate_service_tests]),
    # A queue of its own, so that a change's two authors write their suites side by side.
    ('Component test generation', COMPONENT_TEST_GENERATION_TASK_QUEUE,
     ComponentTestGenerationWorkflow, [generate_component_tests]),
    ('Verification', VERIFICATION_TASK_QUEUE, VerificationWorkflow,
     [verify_service, build_baseline, snapshot_repository_tests, build_repository_tests,
      verify_documentation, build_component_runner, run_component_suite]),
    ('Diagnosis', DIAGNOSIS_TASK_QUEUE, DiagnosisWorkflow, [diagnose_failure]),
    ('Code review', REVIEW_TASK_QUEUE, ReviewWorkflow, [review_changes]),
    ('Documentation', DOCUMENTATION_TASK_QUEUE, DocumentationWorkflow, [write_documentation]),
)


async def main() -> None:
    configure_logging()
    await ensure_settings_schema()
    client = await Client.connect(TEMPORAL_ADDRESS)
    workers = []
    for label, task_queue, workflow_type, activities in STAGES:
        workers.append(Worker(client, task_queue=task_queue, workflows=[workflow_type],
                              activities=activities, max_concurrent_activities=1))
        logger.info('%s worker started on %s', label, task_queue)
    await run_worker(*workers)


if __name__ == '__main__':
    asyncio.run(main())
