"""Deterministic execution policy for single-stage coding workflows."""
import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutType
from temporalio.exceptions import TimeoutError as ActivityTimeout

with workflow.unsafe.imports_passed_through():
    from app.agent_workflows.host import ANSWERS_KEY, RESUME_KEY
    from app.docker_agents import INTERRUPTED_ERROR
    from app.llm.questions import Question, answer_problem

# How long a paused agent waits for the user before its stage fails.
ANSWER_TIMEOUT = timedelta(days=7)
# Signals by which the delivery that started a stage learns of its open questions.
ASKED_SIGNAL = 'agent_asked'
ANSWERED_SIGNAL = 'agent_answered'
# Signal by which it learns that the stage waits for its model provider's limits, with
# the time it goes on (null once it went on).
WAITING_SIGNAL = 'agent_waiting'
# How long a stage waits for its provider between attempts: the wait the provider asked
# for, within these bounds, or else doubling from the first; and how long in all.
PROVIDER_WAIT_MIN = timedelta(minutes=1)
PROVIDER_WAIT_FIRST = timedelta(minutes=5)
PROVIDER_WAIT_MAX = timedelta(hours=2)
PROVIDER_WAIT_TOTAL = timedelta(days=7)


def provider_wait(attempt: int, retry_after: float | None) -> timedelta:
    """How long to wait before the `attempt`-th (from 1) continuation of an agent its
    provider's limits stopped."""
    if retry_after is not None:
        wait = timedelta(seconds=retry_after) + timedelta(seconds=30)
    else:
        wait = PROVIDER_WAIT_FIRST * 2 ** (attempt - 1)
    return max(PROVIDER_WAIT_MIN, min(PROVIDER_WAIT_MAX, wait))


# How often in a row a stage goes on after its worker stopped, before it fails.
MAX_INTERRUPTIONS = 5


def interrupted(exc: ActivityError) -> bool:
    """Whether the activity's worker stopped it: shutting down (as for a redeploy), or
    killed, which leaves its heartbeats to time out."""
    cause = exc.cause
    return ((isinstance(cause, ApplicationError) and cause.type == INTERRUPTED_ERROR)
            or (isinstance(cause, ActivityTimeout) and cause.type == TimeoutType.HEARTBEAT))


async def execute_agent_activity(
    activity_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    session_id: str, context: dict[str, Any],
) -> dict[str, Any]:
    """Run the stage's activity; one its worker stopped runs again, on the next worker,
    continuing (RESUME_KEY) from where its agent was or from the work it saved."""
    interruptions = 0
    while True:
        try:
            return await workflow.execute_activity(
                activity_fn, args=[session_id, context],
                start_to_close_timeout=timedelta(minutes=100),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            if not interrupted(exc) or interruptions >= MAX_INTERRUPTIONS:
                raise
            interruptions += 1
            workflow.logger.warning('Stage activity stopped by its worker; continuing it '
                                    '(%d/%d): %s', interruptions, MAX_INTERRUPTIONS,
                                    exc.cause)
            context = {**context, RESUME_KEY: True}


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
        # Validators of updates that arrive together all pass before either handler runs;
        # only the first one answers.
        if not self._questions:
            raise ApplicationError('The agent is not waiting for answers.')
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
        waited, attempt = timedelta(), 0
        while 'questions' in result or 'provider_limit' in result:
            if 'provider_limit' in result:
                # The provider's limits stopped the agent where it was: it goes on there
                # once they allow, whatever the wait; only the work is the stage's.
                limit = result['provider_limit']
                attempt += 1
                wait = provider_wait(attempt, limit.get('retry_after'))
                if waited + wait > PROVIDER_WAIT_TOTAL:
                    await self._tell_delivery(WAITING_SIGNAL, {
                        'workflow_id': workflow.info().workflow_id, 'stage': self.STAGE,
                        'until': None, 'message': None})
                    raise ApplicationError(
                        f'The model provider stayed unavailable for {PROVIDER_WAIT_TOTAL.days} '
                        f'days: {limit.get("message") or "its limits were reached"}',
                        type='ProviderLimitReached', non_retryable=True)
                until = workflow.now() + wait
                workflow.logger.warning('%s agent waits %s for its provider: %s', self.STAGE,
                                        wait, limit.get('message'))
                await self._tell_delivery(WAITING_SIGNAL, {
                    'workflow_id': workflow.info().workflow_id, 'stage': self.STAGE,
                    'until': until.isoformat(), 'message': limit.get('message')})
                await workflow.sleep(wait)
                waited += wait
                await self._tell_delivery(WAITING_SIGNAL, {
                    'workflow_id': workflow.info().workflow_id, 'stage': self.STAGE,
                    'until': None, 'message': None})
                result = await execute_agent_activity(activity_fn, session_id,
                                                      {**context, RESUME_KEY: True})
                continue
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
