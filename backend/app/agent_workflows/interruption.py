"""Stage activities that their worker stops by shutting down, as for a redeploy, fail with
INTERRUPTED_ERROR: their workflow then runs them again (agent_workflows.workflows), on the
next worker, which goes on from what they saved. Cancelled, they would only fail."""
import asyncio
from typing import Any

from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    ActivityInboundInterceptor, ExecuteActivityInput, Interceptor,
)

from app.docker_agents import INTERRUPTED_ERROR, AgentInterrupted, worker_shutting_down


class _Inbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        try:
            return await super().execute_activity(input)
        except AgentInterrupted as exc:
            raise ApplicationError(str(exc), type=INTERRUPTED_ERROR) from exc
        except asyncio.CancelledError:
            if not worker_shutting_down():
                raise
            raise ApplicationError('Stopped by its worker shutting down',
                                   type=INTERRUPTED_ERROR) from None


class InterruptionInterceptor(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _Inbound(next)
