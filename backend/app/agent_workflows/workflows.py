"""Deterministic execution policy for single-stage coding workflows."""
import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.agent_workflows.host import ANSWERS_KEY
    from app.llm.questions import Question, answer_problem

# How long a paused agent waits for the user before its stage fails.
ANSWER_TIMEOUT = timedelta(days=7)
# Signals by which the delivery that started a stage learns of its open questions.
ASKED_SIGNAL = 'agent_asked'
ANSWERED_SIGNAL = 'agent_answered'


async def execute_agent_activity(
    activity_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    session_id: str, context: dict[str, Any],
) -> dict[str, Any]:
    return await workflow.execute_activity(
        activity_fn, args=[session_id, context],
        start_to_close_timeout=timedelta(minutes=100),
        heartbeat_timeout=timedelta(minutes=2),
        retry_policy=RetryPolicy(maximum_attempts=1),
    )


class AgentWorkflow:
    """One coding stage whose agent may ask the user something. Its run then ends and
    frees the worker; this workflow waits for the answers without holding anything, and a
    new run continues from where the agent stopped. Only this stage's agent sees them."""

    # Which agent this is, as the delivery names it to the user.
    STAGE = ''

    def __init__(self) -> None:
        self._questions: list[Question] = []
        self._answers: list[str] | None = None

    @workflow.query
    def get_questions(self) -> list[Question]:
        return self._questions

    @workflow.update
    async def answer_questions(self, answers: list[str]) -> None:
        self._answers = [answer.strip() for answer in answers]
        self._questions = []

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        if not self._questions:
            raise ValueError('The agent is not waiting for answers.')
        problem = answer_problem(self._questions, answers)
        if problem:
            raise ValueError(problem)

    async def _run_agent(
        self, activity_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        session_id: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        result = await execute_agent_activity(activity_fn, session_id, context)
        while 'questions' in result:
            self._questions, self._answers = result['questions'], None
            workflow.logger.info('%s agent asked %d questions', self.STAGE,
                                 len(self._questions))
            await self._tell_delivery(ASKED_SIGNAL, {
                'workflow_id': workflow.info().workflow_id, 'stage': self.STAGE,
                'questions': self._questions})
            try:
                await workflow.wait_condition(lambda: self._answers is not None,
                                              timeout=ANSWER_TIMEOUT)
            except asyncio.TimeoutError:
                self._questions = []
                await self._tell_delivery(ANSWERED_SIGNAL, workflow.info().workflow_id)
                raise ApplicationError("The agent's questions were not answered in time.",
                                       non_retryable=True)
            await self._tell_delivery(ANSWERED_SIGNAL, workflow.info().workflow_id)
            result = await execute_agent_activity(activity_fn, session_id,
                                                  {**context, ANSWERS_KEY: self._answers})
        return result

    async def _tell_delivery(self, signal: str, argument: Any) -> None:
        parent = workflow.info().parent
        # A stage started on its own has no delivery to show its questions.
        if parent:
            await workflow.get_external_workflow_handle(
                parent.workflow_id, run_id=parent.run_id).signal(signal, argument)
