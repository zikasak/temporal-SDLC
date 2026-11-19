import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from app.temporal.flags import FlowFlags, mark_skipped

with workflow.unsafe.imports_passed_through():
    from app.requirements_review.activities import review_requirements
    from app.requirements_review.models import (
        ROUND_FAILED, Contradiction, ReviewFlowState, ReviewRoundInput, ReviewRoundResult,
    )
    from app.temporal.execution_fields import FLOW_STATE_ATTRIBUTE


@workflow.defn
class RequirementsReviewWorkflow(FlowFlags):
    """The requirements review of one task or onboarding, a child of its task flow. The
    task flow creates it pending; the first round the requirements or onboarding interview
    hands it (`review_round`) starts it. Each round an LLM checks the requirements for
    contradictions, and the result goes back to the interview (`review_result`), which
    asks the user one question per contradiction and hands in the next round once they
    are answered, without a limit on rounds. A round whose LLM call fails for good is
    reported failed with its error; the interview may hand it in again. The review closes
    as passed once a round finds no contradictions, as continued anyway when the user
    finalizes despite them (`finish`, which cancels a running round), or as skipped when
    its task flow skips it (`skip_stage`), such as for a session written because it was
    idle for a day."""

    @workflow.init
    def __init__(self, state: ReviewFlowState) -> None:
        self.state = state
        self._finish: tuple[str, list[Contradiction]] | None = None
        self._skip = False
        self._init_flags()

    def _flags(self) -> tuple[bool | None, bool | None]:
        """Waiting for the user while the contradictions' questions or a failed round wait
        for them, between rounds."""
        return (self.state.status in ("contradictions_found", "failed")
                and not self.state.queued and self._finish is None, None)

    @workflow.query
    def get_state(self) -> ReviewFlowState:
        return self.state

    @workflow.signal
    def review_round(self, round_input: ReviewRoundInput) -> None:
        """Run a round on `round_input`: a new one, or a failed one again."""
        if self._finish is not None or self._skip:
            return
        self.state.queued.append(round_input)

    @workflow.signal
    def finish(self, status: str, contradictions: list[Contradiction]) -> None:
        """The user continued despite the review: close as `status` (continued_anyway)
        with the contradictions that remained, cancelling a running round."""
        if self._finish is None:
            self._finish = (status, list(contradictions or []))

    @workflow.signal
    def skip_stage(self) -> None:
        """Close as skipped: the review will never decide anything."""
        self._skip = True

    @workflow.signal
    def start_stage(self, state: Any = None) -> None:
        """Start the pending review; its rounds arrive by `review_round`."""
        self.state.pending = False

    def _decided(self) -> bool:
        return self._finish is not None or self._skip

    @workflow.run
    async def run(self, state: ReviewFlowState) -> ReviewFlowState:
        if self.state.pending:
            # Created by its task flow before the interview was finalized.
            await workflow.wait_condition(
                lambda: bool(self.state.queued) or self._decided() or not self.state.pending)
            if self._skip and not self.state.queued and self._finish is None:
                return await self._close_skipped()
            self.state.pending = False
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])
            workflow.logger.info("Requirements review started")
        if self.state.task_flow_id:
            self._keep_flags()
        while self.state.status != "passed":
            await workflow.wait_condition(
                lambda: bool(self.state.queued) or self._decided()
                or workflow.info().is_continue_as_new_suggested())
            if self._decided():
                break
            if not self.state.queued:
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
            await self._round(self.state.queued.pop(0))
        if self._skip and self._finish is None and self.state.status != "passed":
            return await self._close_skipped()
        if self._finish is not None and self.state.status != "passed":
            status, contradictions = self._finish
            self.state.status = status  # type: ignore[assignment]
            self.state.contradictions = contradictions
            self.state.error = None
            workflow.logger.info("Requirements review closed as %s with %d contradictions",
                                 status, len(contradictions))
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._close_flags()
        return self.state

    async def _close_skipped(self) -> ReviewFlowState:
        self.state.status = "skipped"
        mark_skipped()
        workflow.logger.info("Requirements review skipped")
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._close_flags()
        return self.state

    async def _round(self, round_input: ReviewRoundInput) -> None:
        """Run the round, unless the user continues anyway first, and report its result."""
        self.state.status = "running"
        self.state.round = round_input.round
        self.state.error = None
        task = asyncio.create_task(self._execute(round_input))
        await workflow.wait_condition(lambda: task.done() or self._decided())
        if not task.done():
            workflow.logger.info("Requirements review round %d cancelled", round_input.round)
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - the cancelled round reports nothing
                pass
            return
        result = task.result()
        self.state.rounds.append({"round": result.round,
                                  "contradictions": len(result.contradictions),
                                  "error": result.error})
        if result.error:
            self.state.status = "failed"
            self.state.error = result.error
        elif result.contradictions:
            self.state.status = "contradictions_found"
            self.state.contradictions = list(result.contradictions)
        else:
            self.state.status = "passed"
            self.state.contradictions = []
        await self._report(result)

    async def _execute(self, round_input: ReviewRoundInput) -> ReviewRoundResult:
        try:
            return await workflow.execute_activity(
                review_requirements, round_input,
                start_to_close_timeout=timedelta(minutes=8),
                schedule_to_close_timeout=timedelta(minutes=20),
                retry_policy=RetryPolicy(maximum_attempts=3,
                                         initial_interval=timedelta(seconds=5)),
            )
        except ActivityError as exc:
            cause = exc.cause
            workflow.logger.warning("Requirements review round %d failed: %s",
                                    round_input.round, cause or exc)
            error = ROUND_FAILED
            if isinstance(cause, ApplicationError) and cause.message:
                error = f"{cause.message} Retry the review round, or continue anyway."
            return ReviewRoundResult(round=round_input.round,
                                     next_number=round_input.next_number, error=error)

    async def _report(self, result: ReviewRoundResult) -> None:
        """Hand the round's result to the interview it reviews."""
        try:
            await workflow.get_external_workflow_handle(self.state.session_id).signal(
                "review_result", result)
        except Exception as exc:  # noqa: BLE001 - an interview gone needs no result
            workflow.logger.warning("Review result not reported to %s: %s",
                                    self.state.session_id, exc)
