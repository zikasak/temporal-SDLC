import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import REVIEW_TASK_QUEUE
from app.review.activities import review_changes
from app.review.workflows import ReviewWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=REVIEW_TASK_QUEUE,
                           workflow_type=ReviewWorkflow, activities=[review_changes],
                           label='Code review')


if __name__ == '__main__':
    asyncio.run(main())
