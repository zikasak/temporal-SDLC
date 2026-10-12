import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import DOCUMENTATION_TASK_QUEUE
from app.documentation.activities import write_documentation
from app.documentation.workflows import DocumentationWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=DOCUMENTATION_TASK_QUEUE,
                           workflow_type=DocumentationWorkflow,
                           activities=[write_documentation], label='Documentation')


if __name__ == '__main__':
    asyncio.run(main())
