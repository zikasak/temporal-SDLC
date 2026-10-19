from typing import Any

from temporalio import workflow

from app.agent_workflows.workflows import AgentWorkflow

with workflow.unsafe.imports_passed_through():
    from app.review.activities import PART_KEY, review_changes
    from app.review.models import MAX_FINDINGS


@workflow.defn
class ReviewWorkflow(AgentWorkflow):
    STAGE = 'review'

    @workflow.run
    async def run(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        # A large change is reviewed in parts, one agent run each; their findings add up.
        result = await self._run_agent(review_changes, session_id, context)
        findings = list(result.get('findings') or [])
        while 'next_part' in result:
            result = await self._run_agent(review_changes, session_id,
                                           {**context, PART_KEY: result['next_part']})
            findings += result.get('findings') or []
        return {**result, 'findings': findings[:MAX_FINDINGS]}
