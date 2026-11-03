from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.routing.activities import route_requirements


@workflow.defn
class ObservabilityWorkflow(AgentWorkflow):
    STAGE = 'routing'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(route_requirements, session_id, context)
