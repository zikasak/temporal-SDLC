import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import IMPLEMENTATION_TASK_QUEUE
from app.implementation.activities import implement_service
from app.implementation.workflows import ImplementationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=IMPLEMENTATION_TASK_QUEUE,
                           workflow_type=ImplementationWorkflow, activities=[implement_service],
                           label='Implementation')


if __name__ == '__main__':
    asyncio.run(main())
