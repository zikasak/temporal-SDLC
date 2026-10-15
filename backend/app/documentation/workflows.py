from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.documentation.activities import write_documentation


@workflow.defn
class DocumentationWorkflow(AgentWorkflow):
    STAGE = 'documentation'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(write_documentation, session_id, context)
