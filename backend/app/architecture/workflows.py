import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.architecture.activities import architecture_turn, write_architecture_document
    from app.architecture.models import (
        FINISHED_STATUSES, SKIP_PROPOSAL_KEY, ArchitectureResult, ArchitectureState,
        architecture_problems, can_skip,
    )
    from app.architecture.source import handed_design
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import execution_fields
    from app.temporal.flags import FlowFlags, PendingStage, mark_skipped
    from app.temporal.interview import Interview, assistant_message, pending_questions
    from app.temporal.workflows import FLOW_ABANDONED, FLOW_ABANDONED_PATCH, _check_out

DISCUSSION_TOO_LONG = "This discussion is too long to continue. Finalize the architecture."
DRAFT_FAILED = "The LLM could not draft the architecture. Retry."
TURN_FAILED = "The LLM could not answer. Retry this message."
NO_SKIP = ("A new service always goes through the architecture stage; only the stage of an "
           "API change can be skipped.")


@workflow.defn
class ArchitectureWorkflow(Interview[ArchitectureState], FlowFlags, PendingStage):
    """One architecture session of a task: the LLM drafts the top-level architecture from
    the requirements (an API change starting from the repository's existing one, or
    proposing to skip the stage instead), and the user reviews it in a conversation of
    updates. Finalizing writes architecture.md and architecture.json; finalizing or
    confirming the skip of an API change ends the session and hands the task's API design
    to the requirements session, whose child it becomes. The session has no idle timeout."""

    INTERVIEWER = "architect"
    TOO_LONG = DISCUSSION_TOO_LONG

    @workflow.init
    def __init__(self, state: ArchitectureState) -> None:
        self.state = state
        if self.state.status not in FINISHED_STATUSES:
            if self._needs_draft():
                self.state.status = "drafting"
            elif self.state.messages and self.state.messages[-1]["role"] == "user":
                self.state.status = "turn_running"
            else:
                self.state.status = "interviewing"
        self._resume_status = self.state.status
        if (self.state.repository and not self.state.checkout
                and self.state.status not in FINISHED_STATUSES):
            self.state.status = "preparing"
        self._updates = 0
        self._turn_lock = asyncio.Lock()
        self._init_flags()
        self._init_pending()

    def _flags(self) -> tuple[bool | None, bool | None]:
        """Waiting for the user while it asks questions or proposes skipping the stage."""
        return (self.state.status == "interviewing"
                and (self.state.skip_proposal is not None
                     or bool(pending_questions(self.state.messages))), None)

    def _needs_draft(self) -> bool:
        """Whether the session waits for its first draft (or the proposal made instead)."""
        return (self.state.structure is None and self.state.skip_proposal is None
                and not any(item["role"] == "assistant" for item in self.state.messages))

    @workflow.run
    async def run(self, state: ArchitectureState) -> ArchitectureResult | None:
        if self.state.pending:
            # Created by its task flow before the requirements were finalized.
            await self._wait_pending()
            return None
        if self.state.task_flow_id:
            self._keep_flags()
        ready = await _check_out(self.state, self._resume_status)
        if ready and self.state.status in ("drafting", "turn_running"):
            async with self._turn_lock:
                await self._turn()
        while self.state.status not in FINISHED_STATUSES:
            seen = self._updates
            await workflow.wait_condition(
                lambda: self._updates != seen
                or self.state.status in FINISHED_STATUSES
                or workflow.info().is_continue_as_new_suggested())
            if (self.state.status not in FINISHED_STATUSES
                    and workflow.info().is_continue_as_new_suggested()):
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._close_flags()
        if self.state.task_flow_id and self.state.status == "skipped":
            # The confirmed skip: the stage never ran, and is listed as skipped.
            mark_skipped()
        if self.state.status == "failed" or not self.state.api_design_session_id:
            if workflow.patched(FLOW_ABANDONED_PATCH):
                # The session ended without handing the task on, so nothing of it will be
                # delivered: it was abandoned, and shows as cancelled.
                await end_cancelled(FLOW_ABANDONED)
            return None
        design = handed_design(self.state, workflow.info().workflow_id)
        if self.state.task_flow_id:
            design.task_flow_id = self.state.task_flow_id
        if workflow.info().parent is None:
            # Without a requirements session to follow it, the session starts the design
            # itself, which outlives it.
            await self._start_design_alone(design)
            return None
        return ArchitectureResult(api_design_session_id=self.state.api_design_session_id,
                                  design=design)

    async def _start_design_alone(self, design: Any) -> None:
        session_id = self.state.api_design_session_id
        assert session_id is not None  # only a session that hands on starts its design
        await workflow.start_child_workflow(
            "ApiDesignWorkflow", design, id=session_id,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            **execution_fields(design.repository))
        workflow.logger.info("API design %s started", session_id)
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            workflow.logger.warning("API design %s not recorded: %s", session_id,
                                    exc.cause or exc)

    @workflow.query
    def get_state(self) -> ArchitectureState:
        return self.state

    def _hand_over(self) -> None:
        # In a task flow, the API design the task flow reserved for the task.
        self.state.api_design_session_id = (self.state.next_session_id
                                            or f"api-design-session-{workflow.uuid4().hex}")

    async def _turn(self) -> bool:
        drafting = self.state.structure is None
        self.state.status = "drafting" if drafting else "turn_running"
        self.state.error = None
        try:
            result = await workflow.execute_activity(
                architecture_turn,
                self.state,
                start_to_close_timeout=timedelta(minutes=8),
                schedule_to_close_timeout=timedelta(minutes=9),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            workflow.logger.warning("Architecture turn failed: %s", exc.cause or exc)
            self.state.error = DRAFT_FAILED if drafting else TURN_FAILED
            self.state.status = "interviewing"
            return False
        message = assistant_message(result)
        if result.get("skip_reason") and can_skip(self.state) and not self.state.skip_declined:
            message[SKIP_PROPOSAL_KEY] = result["skip_reason"]  # type: ignore[literal-required]
            self.state.messages.append(message)
            self.state.skip_proposal = {"reason": result["skip_reason"],
                                        "message_index": len(self.state.messages) - 1}
        else:
            self.state.messages.append(message)
            self.state.structure = result["structure"]
        self.state.status = "interviewing"
        return True

    def _check_open(self) -> None:
        if self.state.status != "interviewing":
            raise ValueError("The architecture session is not waiting for a message.")
        if self.state.messages and self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")

    def _questions_open(self) -> bool:
        # A message while a skip proposal is open declines the proposal instead.
        return super()._questions_open() and self.state.skip_proposal is None

    def _user_replies(self) -> None:
        """A message or answers while a skip proposal is open decline the proposal and
        say what the architecture should cover."""
        if self.state.skip_proposal is not None:
            workflow.logger.info("The user declined skipping the stage")
            self.state.skip_proposal = None
            self.state.skip_declined = True

    def _retryable(self) -> bool:
        return self.state.status == "interviewing" and (
            self._needs_draft()
            or bool(self.state.messages) and self.state.messages[-1]["role"] == "user")

    @workflow.update
    async def retry_message(self) -> None:
        """Run the failed drafting or interviewer turn again."""
        self._updates += 1
        async with self._turn_lock:
            if not self._retryable():
                raise ApplicationError("There is no failed turn to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if not self._retryable():
            raise ValueError("There is no failed drafting or turn to retry.")

    @workflow.update
    async def finalize(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if (self.state.status != "interviewing"
                    or architecture_problems(self.state.structure)):
                raise ApplicationError("The architecture cannot be finalized now.")
            self.state.status = "finalizing"
            self.state.error = None
            try:
                self.state.document = await workflow.execute_activity(
                    write_architecture_document,
                    args=[workflow.info().workflow_id, self.state.structure],
                    start_to_close_timeout=timedelta(minutes=1),
                    schedule_to_close_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError as exc:
                workflow.logger.warning("Architecture document failed: %s", exc.cause or exc)
                self.state.status = "interviewing"
                self.state.error = "The architecture document could not be written. Try again."
                return
            workflow.logger.info("Architecture finalized")
            self.state.skip_proposal = None
            self._hand_over()
            self.state.status = "completed"

    @finalize.validator
    def validate_finalize(self) -> None:
        if self.state.status != "interviewing":
            raise ValueError("Wait for the running turn to finish before finalizing, or the "
                             "session is already finalized or skipped.")
        if self.state.messages and self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before finalizing.")
        problems = architecture_problems(self.state.structure)
        if problems:
            raise ValueError("Fix these problems first: " + " ".join(problems))

    @workflow.update
    async def skip(self) -> None:
        """Skip the stage of an API change: the repository's architecture stays as it is
        and the API design starts with it."""
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or not can_skip(self.state):
                raise ApplicationError("The architecture stage cannot be skipped now.")
            workflow.logger.info("Architecture stage skipped")
            self.state.skip_proposal = None
            # A draft made so far is discarded.
            self.state.structure = None
            self.state.error = None
            self._hand_over()
            self.state.status = "skipped"

    @skip.validator
    def validate_skip(self) -> None:
        if not can_skip(self.state):
            raise ValueError(NO_SKIP)
        if self.state.status in FINISHED_STATUSES or self.state.status == "finalizing":
            raise ValueError("The architecture session is already finalized, skipped or "
                             "failed.")
        if self.state.status != "interviewing":
            raise ValueError("Wait for the drafting or the running turn to finish before "
                             "skipping.")
