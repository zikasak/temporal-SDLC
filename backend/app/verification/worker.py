import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import VERIFICATION_TASK_QUEUE
from app.verification.activities import build_baseline, verify_service
from app.verification.workflows import VerificationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=VERIFICATION_TASK_QUEUE,
                           workflow_type=VerificationWorkflow, activities=[verify_service, build_baseline],
                           label='Verification')


if __name__ == '__main__':
    asyncio.run(main())
