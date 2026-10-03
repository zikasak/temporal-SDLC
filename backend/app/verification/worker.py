import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import VERIFICATION_TASK_QUEUE
from app.verification.activities import verify_service
from app.verification.workflows import VerificationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=VERIFICATION_TASK_QUEUE,
                           workflow_type=VerificationWorkflow, activity_fn=verify_service,
                           label='Verification')


if __name__ == '__main__':
    asyncio.run(main())
