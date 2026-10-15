from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.diagnosis.activities import diagnose_failure


@workflow.defn
class DiagnosisWorkflow(AgentWorkflow):
    STAGE = 'diagnosis'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(diagnose_failure, session_id, context)
