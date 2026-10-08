from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.codegen.activities import generate_server_code


@workflow.defn
class CodegenWorkflow:
    """Turns a session's OpenAPI document into code on disk; started as a child
    of ApiDesignWorkflow once the document is written."""

    @workflow.run
    async def run(
        self, session_id: str, document_id: str, language: str | None = None,
    ) -> dict[str, Any]:
        workflow.logger.info("Generating %s server code for %s", language, session_id)
        server = await workflow.execute_activity(
            generate_server_code,
            args=[session_id, document_id, language],
            start_to_close_timeout=timedelta(minutes=5),
            schedule_to_close_timeout=timedelta(minutes=11),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        return {"server": server}
