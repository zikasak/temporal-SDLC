"""Run one synthetic turn through Temporal, LangGraph and the configured provider.

From backend: APP_TRACE_CONTENT_ENABLED=true python -m tools.trace_smoke
Requires the configured Temporal server; never writes documents or starts builds.
"""
import asyncio
import json
import uuid

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import TEMPORAL_ADDRESS
from app.temporal.activities import requirements_turn, write_requirements_document
from app.temporal.workflows import RequirementsWorkflow
from app.tracing import shutdown_tracing, tracing_client


async def main() -> None:
    langfuse = tracing_client()
    if langfuse is None or not await asyncio.to_thread(langfuse.auth_check):
        raise RuntimeError('Configure valid Langfuse credentials and enable tracing first')
    session = 'langfuse-smoke-' + uuid.uuid4().hex[:12]
    client = await Client.connect(TEMPORAL_ADDRESS)
    # A dedicated queue prevents existing workers from taking the synthetic turn.
    async with Worker(client, task_queue=session, workflows=[RequirementsWorkflow],
                      activities=[requirements_turn, write_requirements_document]):
        handle = await client.start_workflow(RequirementsWorkflow.run, None,
                                             id=session, task_queue=session)
        try:
            # Let run() reach its initial wait before sending the first update.
            # Otherwise an update can race the workflow's fork-resume logic.
            await handle.query(RequirementsWorkflow.get_state)
            await handle.execute_update(RequirementsWorkflow.send_message,
                'For this tracing smoke test, record exactly one functional must-have '
                'requirement: users must be able to search a public book catalog by title. '
                'The fictitious contact is trace-test@example.com. Ask one follow-up question.')
            state = await handle.query(RequirementsWorkflow.get_state)
            if state.error or not state.requirements:
                raise RuntimeError(state.error or 'The smoke turn did not exercise a tool')
            print(json.dumps({'session_id': session, 'requirements': len(state.requirements),
                              'note': 'Query Langfuse observations to confirm ingestion'}))
        finally:
            # Do not finalize: that would write a requirements document to the DB.
            # The terminated smoke history remains visible in Temporal for diagnostics.
            await handle.terminate()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        shutdown_tracing()
