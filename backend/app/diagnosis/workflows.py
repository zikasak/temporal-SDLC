from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import execute_agent_activity

with workflow.unsafe.imports_passed_through():
    from app.diagnosis.activities import diagnose_failure


@workflow.defn
class DiagnosisWorkflow:
    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await execute_agent_activity(diagnose_failure, session_id, context)
