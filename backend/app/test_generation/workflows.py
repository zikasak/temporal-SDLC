from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.test_generation.activities import generate_service_tests


@workflow.defn
class TestGenerationWorkflow(AgentWorkflow):
    STAGE = 'tests'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(generate_service_tests, session_id, context)
