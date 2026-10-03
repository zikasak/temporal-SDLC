import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import TEST_GENERATION_TASK_QUEUE
from app.test_generation.activities import generate_service_tests
from app.test_generation.workflows import TestGenerationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=TEST_GENERATION_TASK_QUEUE,
                           workflow_type=TestGenerationWorkflow, activity_fn=generate_service_tests,
                           label='Test generation')


if __name__ == '__main__':
    asyncio.run(main())
