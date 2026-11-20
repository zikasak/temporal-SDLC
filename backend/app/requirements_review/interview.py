"""The interview's side of the requirements review, shared by requirements sessions and
onboardings. Safe to import from workflow code."""

from typing import TYPE_CHECKING, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.requirements_review.models import (
        CLEARED, QUESTIONS_OPEN, ROUND_RUNNING, UNREACHABLE, RequirementsReview,
        ReviewRoundInput, ReviewRoundResult, contradiction_message, previous_round,
        requirements_diff,
    )
    from app.temporal.interview import pending_questions


class ReviewedInterview:
    """An interview whose requirements its task's RequirementsReviewWorkflow reviews before
    the requirements document is written. The workflow holds `state` with `review`
    (RequirementsReview), `review_id`, `requirements`, `messages` and `status`, counts its
    updates in `_updates`, queues turns on `_turn_lock` and defines (rounds go to the review
    through the task flow, `task_flow_id`, which starts the pending review with the first):

    - `_review_applies()`: whether the review runs for it now;
    - `_review_existing()`: the existing requirements the round checks against, and
      whether the activity reads them from the checkout instead;
    - `_review_cleared()`: what follows once the review passed: write the document."""

    state: Any
    _updates: int

    if TYPE_CHECKING:
        def _review_applies(self) -> bool: ...

        async def _review_existing(self) -> tuple[list[Any], bool]: ...

        async def _review_cleared(self) -> None: ...

    def _review(self) -> RequirementsReview:
        if self.state.review is None:
            self.state.review = RequirementsReview()
        return self.state.review

    def _review_running(self) -> bool:
        return self._review_applies() and self._review().status == "running"

    def _review_questions_open(self) -> bool:
        """Whether the last message asks the review's contradiction questions."""
        return (self._review_applies() and self._review().questions_open
                and bool(pending_questions(self.state.messages)))

    def _review_check(self, force: bool) -> None:
        """Raise ValueError unless finalizing (with `force` or not) may go on now as far
        as the review is concerned."""
        if force or not self._review_applies():
            return
        if self._review().status == "running":
            raise ValueError(ROUND_RUNNING)
        if self._review_questions_open():
            raise ValueError(QUESTIONS_OPEN)

    def _review_answers(self, answers: list[str]) -> None:
        """The user answered the contradiction questions, before the answers are added:
        the next round starts after the turn that applies them."""
        if not self._review_questions_open():
            return
        review = self._review()
        review.answers = list(answers)
        review.after_turn = True

    def _review_replied(self) -> None:
        """The user replied: the contradiction questions can no longer be answered."""
        if self.state.review is not None:
            self.state.review.questions_open = False

    async def _review_after_turn(self, ok: bool) -> None:
        """Start the next round once the turn applied the answers without asking more."""
        review = self.state.review
        if (not ok or review is None or not review.after_turn or not self._review_applies()
                or self.state.status != "interviewing"
                or pending_questions(self.state.messages)):
            return
        await self._start_review_round()

    async def _start_review_round(self, retry: bool = False) -> None:
        """Hand the review its next round, or with `retry` the failed one again."""
        review = self._review()
        self.state.status = "finalizing"
        self.state.error = None
        if retry and review.round_input is not None:
            round_input = review.round_input
        else:
            existing, read_existing = await self._review_existing()
            requirements = list(self.state.requirements)
            round_input = ReviewRoundInput(
                session_id=workflow.info().workflow_id, round=review.round + 1,
                requirements=requirements, existing=existing,
                diff=requirements_diff(review.reviewed, requirements),
                previous=previous_round(review), next_number=review.next_number,
                read_existing=read_existing)
        review.round = round_input.round
        review.round_input = round_input
        review.status = "running"
        review.error = None
        review.questions_open = False
        review.after_turn = False
        workflow.logger.info("Requirements review round %d handed in", round_input.round)
        try:
            # The task flow starts its pending review with the first round.
            await workflow.get_external_workflow_handle(self.state.task_flow_id).signal(
                "review_round", round_input)
        except Exception as exc:  # noqa: BLE001 - reported like a failed round
            workflow.logger.warning("Requirements review not reached: %s", exc)
            review.status = "failed"
            review.error = UNREACHABLE
            self.state.status = "interviewing"

    async def _review_result(self, result: ReviewRoundResult) -> None:
        """A round's result: questions for its contradictions, the failure, or the
        document once no contradiction remains."""
        review = self._review()
        if review.status != "running" or result.round != review.round:
            workflow.logger.info("Stale requirements review result of round %d ignored",
                                 result.round)
            return
        self._updates += 1
        if result.error:
            review.status = "failed"
            review.error = result.error
            review.contradictions = []
            self.state.status = "interviewing"
            return
        round_input = review.round_input
        review.reviewed = list(round_input.requirements if round_input
                               else self.state.requirements)
        review.round_input = None
        review.next_number = max(review.next_number, result.next_number)
        review.answers = None
        review.error = None
        review.contradictions = list(result.contradictions)
        review.remaining = list(result.contradictions)
        if result.contradictions:
            review.status = "contradictions_found"
            review.questions_open = True
            self.state.messages.append(contradiction_message(result.contradictions))
            self.state.status = "interviewing"
            workflow.logger.info("Requirements review found %d contradictions",
                                 len(result.contradictions))
            return
        review.status = "passed"
        workflow.logger.info("Requirements review passed")
        await self._review_cleared()

    async def _force_review(self) -> None:
        """Continue despite the review: it closes as continued anyway with the last
        finished round's contradictions, cancelling a running round."""
        review = self._review()
        remaining = list(review.remaining)
        review.status = "continued_anyway"
        review.contradictions = remaining
        review.questions_open = False
        review.error = None
        review.after_turn = False
        review.round_input = None
        workflow.logger.info("Continuing despite %d contradictions", len(remaining))
        try:
            await workflow.get_external_workflow_handle(self.state.task_flow_id).signal(
                "review_finish", args=["continued_anyway", remaining])
        except Exception as exc:  # noqa: BLE001 - its task flow closes it as skipped then
            workflow.logger.warning("Requirements review not told to finish: %s", exc)

    def _review_skipped(self) -> None:
        """No review runs, such as for a document written because the session was idle."""
        if self.state.review is not None and self.state.review.status not in CLEARED:
            self.state.review.status = "skipped"
            self.state.review.questions_open = False
            self.state.review.after_turn = False

    def _review_reported(self) -> bool:
        """Whether the review decided, so the task flow lets it close on its own."""
        return self._review_applies() and self._review().status in CLEARED

    def _remaining_contradictions(self) -> list[Any]:
        """The contradictions the requirements document lists: those the user continued
        despite."""
        review = self.state.review
        if review is None or review.status != "continued_anyway":
            return []
        return list(review.contradictions)
