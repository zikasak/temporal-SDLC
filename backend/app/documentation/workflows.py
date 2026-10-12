from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import execute_agent_activity

with workflow.unsafe.imports_passed_through():
    from app.documentation.activities import write_documentation


@workflow.defn
class DocumentationWorkflow:
    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await execute_agent_activity(write_documentation, session_id, context)
