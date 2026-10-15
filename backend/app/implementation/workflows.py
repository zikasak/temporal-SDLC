from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.implementation.activities import implement_service


@workflow.defn
class ImplementationWorkflow(AgentWorkflow):
    STAGE = 'implementation'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(implement_service, session_id, context)
