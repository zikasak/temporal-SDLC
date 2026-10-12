import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import VERIFICATION_TASK_QUEUE
from app.verification.activities import (
    build_baseline, build_repository_tests, snapshot_repository_tests, verify_documentation,
    verify_service,
)
from app.verification.workflows import VerificationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=VERIFICATION_TASK_QUEUE,
                           workflow_type=VerificationWorkflow, activities=[verify_service, build_baseline, snapshot_repository_tests,
                                       build_repository_tests, verify_documentation],
                           label='Verification')


if __name__ == '__main__':
    asyncio.run(main())
