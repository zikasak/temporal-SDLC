from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.review.activities import review_changes


@workflow.defn
class ReviewWorkflow(AgentWorkflow):
    STAGE = 'review'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return await self._run_agent(review_changes, session_id, context)
