import asyncio

from app.agent_workflows.worker import run_agent_worker
from app.config import DIAGNOSIS_TASK_QUEUE
from app.diagnosis.activities import diagnose_failure
from app.diagnosis.workflows import DiagnosisWorkflow


async def main() -> None:
    await run_agent_worker(task_queue=DIAGNOSIS_TASK_QUEUE,
                           workflow_type=DiagnosisWorkflow, activity_fn=diagnose_failure,
                           label='Diagnosis')


if __name__ == '__main__':
    asyncio.run(main())
