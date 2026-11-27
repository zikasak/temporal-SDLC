import asyncio
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError, ApplicationError, CancelledError, ChildWorkflowError,
)

from app.temporal.flags import FlowFlags, OpenStage, PendingStage
from app.temporal.interview import Interview, assistant_message, pending_questions
from app.requirements_review.interview import ReviewedInterview

with workflow.unsafe.imports_passed_through():
    from app.tasks.models import DesignResult, RequirementsResult
    from app.api_design.models import ApiDesignState
    from app.api_design.openapi import api_problems, design_problems
    from app.api_design.prompt import opening_fallback
    from app.api_design.source import design_state
    from app.architecture.models import ARCHITECTURE_KINDS, ArchitectureResult, ArchitectureState
    from app.architecture.source import architecture_state
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryState, TaskSpec, delivery_id
    from app.github.models import RepositorySelection
    from app.temporal.closing import end_cancelled
    from app.temporal.deadline import CHAT_ANSWER_HEARTBEAT, CHAT_ANSWER_TIMEOUT
    from app.temporal.execution_fields import execution_fields, task_fields
    from app.requirements.document import render_requirements_markdown
    from app.requirements.models import (
        CHANGE_KINDS, CONTRACT_KINDS, DIRECT_KINDS, KIND_PROBLEM, RequirementsState,
        numbered_after,
    )
    from app.requirements.prompt import opening_message
    from app.requirements_review.models import (
        CLEARED, REVIEWED_KINDS, ROUND_RUNNING, ReviewRoundResult,
    )
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        parse_document,
        read_repository_contract,
        read_requirements_document,
        register_session,
        requirements_turn,
        send_llm_message,
        update_repository_checkout,
        write_openapi_document,
        write_requirements_document,
    )

SESSION_IDLE_TIMEOUT = timedelta(hours=24)
# How long a Chat message may wait for its session's repository.
REPOSITORY_CHECKOUT_TIMEOUT = timedelta(minutes=10)
OPEN_STATUSES = ("interviewing", "turn_running")
INTERVIEW_TOO_LONG = "This interview is too long to continue. Generate the document."
DISCUSSION_TOO_LONG = "This discussion is too long to continue. Generate the OpenAPI."
# Patches whose old code paths are gone; histories recorded with their markers replay
# through workflow.deprecate_patch, called where workflow.patched was.
TASK_TREE = "task-tree"
AUTO_API_DESIGN = "auto-api-design"
ARCHITECTURE_STAGE = "architecture-stage"
# Marks the workflow code that ends a flow's sessions as soon as all of its parts have
# ended, as cancelled when its delivery was, rather than waiting idle after them.
FLOW_ENDS_PATCH = "flow-ends-with-delivery"
FLOW_CANCELLED = "The flow's pull request was closed without being merged."
# Marks the workflow code that ends a flow as cancelled as soon as its parts have all
# ended without anything delivered, rather than waiting for a fork or a new design.
FLOW_ABANDONED_PATCH = "flow-ends-without-delivery"
FLOW_ABANDONED = "The flow ended without a delivery."
# Marks the workflow code that decides from the state a run started with whether it opens
# by answering a user message, rather than from the messages as the run first reaches
# that point: an update handed over with the start has appended its own message by then
# and answers it itself, so the run answered it a second time.
ANSWER_ONCE_PATCH = "answer-start-message-once"


def _ends_with_user_message(state: RequirementsState | ApiDesignState) -> bool:
    return bool(state.messages) and state.messages[-1]["role"] == "user"


def _opens_with_answer(started_unanswered: bool,
                       state: RequirementsState | ApiDesignState) -> bool:
    """Whether the run opens by answering the user message its state started with, as a
    session forked after that message, or continued as new after its turn failed, does."""
    if workflow.patched(ANSWER_ONCE_PATCH):
        return started_unanswered
    return _ends_with_user_message(state)


def expiry_ends_with_status(expired: bool) -> bool:
    """A session that expired idle delivered nothing, so its flow was abandoned and
    ends as cancelled like any other flow that delivered nothing: never with its own
    status. Its state still records why it expired."""
    return False


def _await_checkout(state: RequirementsState | ApiDesignState) -> str:
    """Hold a new session in `preparing` until its repository is checked out, so no
    message reaches the model first. Returns the status to continue with."""
    status = state.status
    if state.repository and not state.checkout and status in OPEN_STATUSES:
        state.status = "preparing"
    return status


async def _check_out(state: RequirementsState | ApiDesignState | ArchitectureState,
                     resume_status: str) -> bool:
    """Check out the repository of a `preparing` session; False when it cannot go on."""
    if state.status != "preparing":
        return True
    workflow.deprecate_patch("repository-checkout")
    try:
        state.checkout = await workflow.execute_activity(
            checkout_repository,
            args=[workflow.info().workflow_id, state.repository],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
    except ActivityError as exc:
        workflow.logger.warning("Repository checkout failed: %s", exc.cause or exc)
        state.status = "failed"
        state.error = ("The repository could not be checked out. Check the GitHub "
                       "connection and start a new session.")
        return False
    # A status of the session's own kind, which the caller resumes it in.
    state.status = cast(Any, resume_status)
    return True


class _Delivering:
    """A session that delivers its task as its own child, and stays open until that
    closes. A delivery stopped from outside, such as a terminated one, leaves the session
    open for the user to start it again (`restart_delivery`) from where it stopped."""

    def _init_delivery(self) -> None:
        self._delivering = False
        self._redelivery: DeliveryState | None = None
        # Set when the delivery ended cancelled, its pull request closed without merging.
        self._delivery_cancelled = False

    async def _run_delivery(self, state: DeliveryState) -> None:
        session_id = workflow.info().workflow_id
        self._delivering = True
        while True:
            stopped = False
            try:
                await workflow.execute_child_workflow(
                    "DeliveryWorkflow", state, id=delivery_id(session_id),
                    **task_fields(state.spec))
            except ChildWorkflowError as exc:
                # Stage failures are results of the delivery; only an aborted one ends here.
                workflow.logger.warning("Delivery did not finish: %s", exc.cause or exc)
                stopped = True
                if (isinstance(exc.cause, CancelledError)
                        and workflow.patched(FLOW_ENDS_PATCH)):
                    # Its pull request was closed without merging: the flow ends with it.
                    self._delivering = False
                    self._delivery_cancelled = True
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    return
            workflow.deprecate_patch(TASK_TREE)
            if stopped and self._redelivery is None:
                try:
                    await workflow.wait_condition(lambda: self._redelivery is not None,
                                                  timeout=TASK_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
            if self._redelivery is None:
                self._delivering = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                if self._redelivery is None:
                    return
            state, self._redelivery = self._redelivery, None
            self._delivering = True
            workflow.logger.info("Starting the stopped delivery again")

    def _restart_delivery(self, state: DeliveryState) -> None:
        self._redelivery = state

    def _validate_restart_delivery(self) -> None:
        # The caller found the delivery closed; this session may not have seen it yet.
        if not self._delivering or self._redelivery is not None:
            raise ValueError("This session's delivery cannot be started again.")


@workflow.defn
class LLMRequestWorkflow:
    @workflow.run
    async def run(
        self, messages: Any, parse_document_id: str | None = None,
        session_id: str | None = None, repository: RepositorySelection | None = None,
    ) -> str:
        workflow.logger.info("LLM request started (session %s, parse document %s)",
                             session_id, parse_document_id)
        if repository:
            # Only a session's first message clones; later ones find the working copy.
            try:
                await workflow.execute_activity(
                    checkout_repository,
                    args=[session_id or workflow.info().workflow_id, repository],
                    start_to_close_timeout=REPOSITORY_CHECKOUT_TIMEOUT,
                    schedule_to_close_timeout=REPOSITORY_CHECKOUT_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError as exc:
                # The model still answers, and tells the user what it could not read.
                workflow.logger.warning("Repository checkout failed: %s", exc.cause or exc)
        if parse_document_id:
            await workflow.execute_activity(
                parse_document,
                parse_document_id,
                start_to_close_timeout=timedelta(seconds=90),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        return await workflow.execute_activity(
            send_llm_message,
            # The request this one answers, whose saved provider conversation it continues.
            args=[messages, session_id, repository,
                  workflow.memo_value("parent_request_id", "", type_hint=str)],
            start_to_close_timeout=CHAT_ANSWER_TIMEOUT,
            schedule_to_close_timeout=CHAT_ANSWER_TIMEOUT + timedelta(minutes=1),
            heartbeat_timeout=CHAT_ANSWER_HEARTBEAT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


def _interview_flags(state: RequirementsState | ApiDesignState) -> tuple[bool, None]:
    """An interview waits for the user while its last message asks unanswered questions."""
    return (state.status == "interviewing" and bool(pending_questions(state.messages)),
            None)


@workflow.defn
class RequirementsWorkflow(Interview[RequirementsState], ReviewedInterview, _Delivering,
                           FlowFlags, OpenStage):
    """One requirements interview: each user message is an update, and the
    finalize update turns the gathered requirements into a document. A task that
    leaves the API as it is has no API design to follow, so the session then hands
    it to a DeliveryWorkflow itself; any other task's API designs, and through them
    their deliveries, are this session's children. The session is the root of its
    task, open while any part of it is.

    In a task flow, finalizing a new service, an API change or a bugfix first hands the
    requirements to the task's requirements review (RequirementsReviewWorkflow): each
    contradiction it finds is asked as a question, the answers lead to the next round, and
    the document is written once a round finds none, or when the user finalizes with
    force despite them."""

    INTERVIEWER = "analyst"
    TOO_LONG = INTERVIEW_TOO_LONG

    @workflow.init
    def __init__(self, state: RequirementsState | None = None) -> None:
        self.state = state or RequirementsState(messages=[opening_message()])
        # A session forked after a user message starts by answering it.
        self._started_unanswered = _ends_with_user_message(self.state)
        if self._started_unanswered:
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        # Validators can all pass in one workflow task, so turns queue here.
        self._turn_lock = asyncio.Lock()
        self._init_delivery()
        # The API design sessions started from this one, each followed to its end.
        self._designs: list[asyncio.Task[None]] = []
        self._owns_designs = False
        # How many of those ended completed, and how many cancelled.
        self._designs_completed = 0
        self._designs_cancelled = 0
        # How many ended after their delivery closed, returning their document; a design
        # that expired or failed returns none.
        self._designs_delivered = 0
        # Set when the user generated the document, rather than an idle session expiring.
        self._finalized = False
        # Set when the session expired after a day without messages.
        self._expired = False
        self._init_flags()
        self._init_open_stage()

    def _flags(self) -> tuple[bool | None, bool | None]:
        waiting, unresolved = _interview_flags(self.state)
        # A failed review round waits for the user to retry it or continue anyway.
        failed = (self.state.status == "interviewing" and self._review_applies()
                  and self._review().status == "failed")
        return waiting or failed, unresolved

    # The requirements review.

    def _review_applies(self) -> bool:
        return bool(self.state.review_id) and self.state.kind in REVIEWED_KINDS

    async def _review_existing(self) -> tuple[list[Any], bool]:
        """The existing service's requirements the task does not retire."""
        contract = self.state.contract
        if not contract or self.state.kind not in CHANGE_KINDS:
            return [], False
        ended = {item["id"] for item in self.state.retired}
        return [item for item in contract.get("requirements") or []
                if item.get("id") not in ended], False

    async def _review_cleared(self) -> None:
        self._finalized = await self._write_document()

    def _questions_open(self) -> bool:
        # A message may drop the review's contradiction questions instead of answering.
        if self._review_questions_open():
            return False
        return super()._questions_open()

    def _answers_given(self, answers: list[str]) -> None:
        self._review_answers(answers)

    def _user_replies(self) -> None:
        self._review_replied()

    async def _after_turn(self, ok: Any) -> None:
        await self._review_after_turn(bool(ok))

    @workflow.signal
    async def review_result(self, result: ReviewRoundResult) -> None:
        """The result of a round of the task's requirements review."""
        async with self._turn_lock:
            await self._review_result(result)

    @workflow.update
    async def retry_review(self) -> None:
        """Run the failed review round again with its original input."""
        self._updates += 1
        async with self._turn_lock:
            if not self._review_failed():
                raise ApplicationError("The last requirements review round did not fail.")
            await self._start_review_round(retry=True)

    @retry_review.validator
    def validate_retry_review(self) -> None:
        if not self._review_failed():
            raise ValueError("The last requirements review round did not fail.")

    def _review_failed(self) -> bool:
        return (self.state.status == "interviewing" and self._review_applies()
                and self._review().status == "failed")

    @workflow.run
    async def run(self, state: RequirementsState | None = None) -> Any:
        if self.state.task_flow_id:
            self._keep_flags()
        ready = await _check_out(self.state, self._resume_status)
        if ready and _opens_with_answer(self._started_unanswered, self.state):
            async with self._turn_lock:
                await self._turn()
        while self.state.status not in {"completed", "failed"}:
            seen = self._updates
            try:
                await workflow.wait_condition(
                    lambda: self._updates != seen
                    or self.state.status in {"completed", "failed"}
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=SESSION_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self.state.status == "interviewing":
                    await self._expire()
                continue
            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.state.task_flow_id:
            # A stage of a task flow hands the task on to it; the task flow starts the
            # next stage. The session stays open as long as the task flow does.
            return await self._stay_open(await self._hand_off())
        if self.state.status == "completed" and self.state.kind in DIRECT_KINDS:
            await self._deliver()
            if self._delivery_cancelled:
                await end_cancelled(FLOW_CANCELLED)
        elif self.state.status == "completed":
            workflow.deprecate_patch(TASK_TREE)
            if self._finalized and self.state.repository:
                workflow.deprecate_patch(AUTO_API_DESIGN)
                if self.state.kind in ARCHITECTURE_KINDS:
                    workflow.deprecate_patch(ARCHITECTURE_STAGE)
                    await self._start_first_architecture()
                else:
                    await self._start_first_design()
            await self._own_designs()
            if self._designs_cancelled and not self._designs_completed:
                await end_cancelled(FLOW_CANCELLED)
            if (not self._designs_delivered and workflow.patched(FLOW_ABANDONED_PATCH)
                    and not (not self._designs and expiry_ends_with_status(self._expired))):
                # Nothing of the flow was delivered: it was abandoned, even a session
                # that expired and kept its document, starting nothing.
                await end_cancelled(FLOW_ABANDONED)
        elif (self.state.status == "failed" and workflow.patched(FLOW_ABANDONED_PATCH)
              and not expiry_ends_with_status(self._expired)):
            # The session could not go on, or expired, before anything was delivered:
            # it was abandoned, with the reason in its state.
            await end_cancelled(FLOW_ABANDONED)
        return self.state.document

    async def _own_designs(self) -> None:
        """Stay open while any API design of this task is, and for as long after the
        last one as a finished delivery waits for the user."""
        self._owns_designs = True
        while True:
            await workflow.wait_condition(
                lambda: all(task.done() for task in self._designs)
                and workflow.all_handlers_finished())
            ends = (self._designs and workflow.patched(FLOW_ENDS_PATCH)
                    and (self._designs_delivered or self._designs_cancelled))
            # A flow whose parts all ended with no delivery, such as an API design that
            # expired or an architecture session that ended without handing on, or a
            # session that expired with no part started, ends too, as cancelled.
            if not ends and workflow.patched(FLOW_ABANDONED_PATCH):
                ends = True
            if ends:
                # Every part of the flow has ended: so does the flow, at once.
                self._owns_designs = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                if all(task.done() for task in self._designs):
                    workflow.logger.info("Every part of the flow ended, closing its task")
                    return
                self._owns_designs = True
                continue
            started = len(self._designs)
            try:
                await workflow.wait_condition(lambda: len(self._designs) != started,
                                              timeout=TASK_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                self._owns_designs = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                if all(task.done() for task in self._designs):
                    workflow.logger.info("Requirements session idle, closing its task")
                    return
                self._owns_designs = True

    async def _start_first_design(self) -> None:
        """Hand the finished requirements to their API design, which the user then
        discusses without having to start it."""
        session_id = f"api-design-session-{workflow.uuid4().hex}"
        state = design_state(workflow.info().workflow_id, self.state,
                             await self._requirements_text())
        await self._start_design(session_id, state)
        self.state.api_design_session_id = session_id
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # Unrecorded, the design is still listed and served while Temporal is up.
            workflow.logger.warning("API design %s not recorded: %s", session_id,
                                    exc.cause or exc)

    async def _start_design(self, session_id: str, state: ApiDesignState) -> None:
        handle = await workflow.start_child_workflow(
            "ApiDesignWorkflow", state, id=session_id, **execution_fields(state.repository))
        workflow.logger.info("API design %s started", session_id)
        self._designs.append(asyncio.create_task(self._follow_design(handle)))

    async def _register(self, session_id: str) -> None:
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # Unrecorded, the session is still listed and served while Temporal is up.
            workflow.logger.warning("Session %s not recorded: %s", session_id,
                                    exc.cause or exc)

    async def _start_first_architecture(self) -> None:
        """Hand the finished requirements to their architecture session, which drafts the
        architecture at once and later hands the task on to its API design."""
        session_id = f"architecture-session-{workflow.uuid4().hex}"
        state = architecture_state(workflow.info().workflow_id, self.state,
                                   await self._requirements_text())
        await self._start_architecture(session_id, state)
        self.state.architecture_session_id = session_id
        await self._register(session_id)

    async def _start_architecture(self, session_id: str, state: ArchitectureState) -> None:
        handle = await workflow.start_child_workflow(
            "ArchitectureWorkflow", state, id=session_id, result_type=ArchitectureResult,
            **execution_fields(state.repository))
        workflow.logger.info("Architecture session %s started", session_id)
        self._designs.append(asyncio.create_task(self._follow_architecture(handle)))

    async def _follow_architecture(self, handle: workflow.ChildWorkflowHandle) -> None:
        """Wait for the architecture session to be finalized or skipped, then start the API
        design it hands on as this session's child, next to it."""
        try:
            result = await handle
        except ChildWorkflowError as exc:
            workflow.logger.warning("Architecture session %s did not finish: %s", handle.id,
                                    exc.cause or exc)
            return
        if not result:
            return
        if isinstance(result, dict):
            result = ArchitectureResult(
                api_design_session_id=result["api_design_session_id"],
                design=ApiDesignState(**result["design"]))
        if workflow.patched("register-design-first"):
            # Recorded before it starts, so that the design is listed as soon as it runs.
            await self._register(result.api_design_session_id)
            await self._start_design(result.api_design_session_id, result.design)
        else:
            # Histories recorded before the change started the design first.
            await self._start_design(result.api_design_session_id, result.design)
            await self._register(result.api_design_session_id)

    @workflow.update
    async def start_architecture(self, session_id: str, state: ArchitectureState) -> None:
        """Start an architecture session of this task, such as a fork of one, as this
        session's child; done once it has started."""
        await self._start_architecture(session_id, state)

    @start_architecture.validator
    def validate_start_architecture(self, session_id: str, state: ArchitectureState) -> None:
        if not self._owns_designs:
            raise ValueError("Generate the requirements document before the architecture.")

    @workflow.update
    async def start_api_design(self, session_id: str, state: ApiDesignState) -> None:
        """Start an API design of this task, or a fork of one, as this session's child;
        done once it has started."""
        await self._start_design(session_id, state)

    @start_api_design.validator
    def validate_start_api_design(self, session_id: str, state: ApiDesignState) -> None:
        if not self._owns_designs:
            raise ValueError("Generate the requirements document before designing the API.")

    async def _follow_design(self, handle: workflow.ChildWorkflowHandle) -> None:
        try:
            result = await handle
        except ChildWorkflowError as exc:
            workflow.logger.warning("API design %s did not finish: %s", handle.id,
                                    exc.cause or exc)
            if isinstance(exc.cause, CancelledError):
                # Its delivery's pull request was closed without merging.
                self._designs_cancelled += 1
        else:
            self._designs_completed += 1
            if result:
                # It returns its document only once it was finalized and delivered: its
                # delivery ended with its pull request, or after a push that opened none.
                self._designs_delivered += 1

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this session's stopped delivery again from `state`."""
        self._restart_delivery(state)

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        self._validate_restart_delivery()

    @workflow.query
    def get_state(self) -> RequirementsState:
        return self.state

    def _check_open(self) -> None:
        if self._review_running():
            raise ValueError(ROUND_RUNNING)
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
                raise ApplicationError("There is no unanswered message to retry.")
            await self._after_turn(await self._turn())

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
            raise ValueError("There is no unanswered message to retry.")

    @workflow.update
    async def finalize(self, force: bool = False) -> None:
        """Write the requirements document; for a task its requirements review clears
        first, unless `force` continues despite it."""
        self._updates += 1
        async with self._turn_lock:
            if not (self.state.status == "interviewing"
                    or force and self._review_running()):
                raise ApplicationError("This session is no longer accepting changes.")
            workflow.logger.info("Finalize requested%s", " with force" if force else "")
            if self._review_applies() and self._review().status not in CLEARED:
                if not force:
                    self.state.status = "finalizing"
                    self.state.error = None
                    if not await self._prepare_contract():
                        self.state.status = "interviewing"
                        return
                    await self._start_review_round()
                    return
                await self._force_review()
            elif self.state.review_id and not self._review_applies():
                # A documentation task is not reviewed.
                self._review_skipped()
            self._finalized = await self._write_document()

    @finalize.validator
    def validate_finalize(self, force: bool = False) -> None:
        if not self.state.kind:
            # Also with force: the task flow creates its later stages for the kind.
            raise ValueError(KIND_PROBLEM)
        if not (force and self._review_running()):
            if (self.state.status != "interviewing"
                    or self.state.messages[-1]["role"] == "user"):
                if self._review_running():
                    raise ValueError(ROUND_RUNNING)
                raise ValueError(
                    "Retry the unanswered message before generating the document.")
            self._review_check(force)
        if not self.state.requirements:
            raise ValueError("No requirements have been recorded yet.")

    @workflow.update
    async def update_checkout(self) -> None:
        """Check the session's branch out again at its latest commit: what the interview
        reads and the task is finalized against from then on, such as a commit that added
        the contract the task needs."""
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting changes.")
            self.state.status = "preparing"
            self.state.error = None
            try:
                self.state.checkout = await workflow.execute_activity(
                    update_repository_checkout,
                    args=[workflow.info().workflow_id, self.state.repository],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError as exc:
                # The earlier working copy stays until a new one is complete.
                workflow.logger.warning("Repository checkout update failed: %s",
                                        exc.cause or exc)
                self.state.error = ("The repository could not be checked out again. Check "
                                    "the GitHub connection and try again.")
            else:
                workflow.logger.info("Checked out again at %s", self.state.checkout["commit"])
            finally:
                self.state.status = "interviewing"

    @update_checkout.validator
    def validate_update_checkout(self) -> None:
        if not self.state.repository or not self.state.checkout:
            raise ValueError("This session has no repository checkout to update.")
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before updating the checkout.")

    async def _turn(self) -> bool:
        self.state.status = "turn_running"
        self.state.error = None
        try:
            result = await workflow.execute_activity(
                requirements_turn,
                args=[self.state.messages, self.state.requirements, self.state.kind,
                      self.state.retired],
                start_to_close_timeout=timedelta(minutes=6),
                schedule_to_close_timeout=timedelta(minutes=7),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            workflow.logger.warning("Requirements turn failed: %s", exc.cause or exc)
            self.state.error = "The LLM could not answer. Retry this message."
            ok = False
        else:
            self.state.messages.append(assistant_message(result))
            self.state.requirements = result["requirements"]
            self.state.kind = result["kind"]
            self.state.retired = result["retired"]
            ok = True
        self.state.status = "interviewing"
        return ok

    async def _read_contract(self) -> bool:
        """A task on the service a repository was published with starts from that
        contract, so it cannot be specified for a repository without one."""
        try:
            contract = await workflow.execute_activity(
                read_repository_contract,
                workflow.info().workflow_id,
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Repository contract unavailable: %s", exc.cause or exc)
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type == "ContractError":
                self.state.error = cause.message
            else:
                self.state.error = "The repository's contract could not be read. Try again."
            return False
        # The session's state keeps the contract as the plain mapping it was recorded as.
        self.state.contract = cast(dict[str, Any], contract)
        checkout = self.state.checkout
        assert checkout is not None  # the repository was checked out before the contract
        self.state.contract_commit = checkout["commit"]
        if self.state.kind not in CHANGE_KINDS:
            # A documentation task leaves the contract as it is.
            return True
        # The task's requirements join the contract's when it is published. The interview
        # numbers them past the contract's already; this holds whatever it was given.
        self.state.requirements = numbered_after(
            self.state.requirements,
            [item["id"] for item in contract["requirements"]])
        # Only what this contract requires can be ended, and as the contract states it.
        ended = {item["id"] for item in self.state.retired}
        self.state.retired = [item for item in contract["requirements"]
                              if item["id"] in ended]
        return True

    async def _prepare_contract(self) -> bool:
        """Read the contract a task on an existing service is specified against; False
        when it cannot be read, with the reason in the state's error."""
        # Only a task on an existing service keeps a contract, whatever kind it was before,
        # and only a change to that service ends requirements of it.
        self.state.contract = None
        if self.state.kind not in CHANGE_KINDS:
            self.state.retired = []
        if self.state.kind in CONTRACT_KINDS and not await self._read_contract():
            return False
        return True

    async def _write_document(self) -> bool:
        self.state.status = "finalizing"
        self.state.error = None
        if not await self._prepare_contract():
            self.state.status = "interviewing"
            return False
        args: list[Any] = [workflow.info().workflow_id, self.state.messages,
                           self.state.requirements, self.state.retired]
        contradictions = self._remaining_contradictions()
        if contradictions:
            # Those the user continued despite are listed in the document.
            args.append(contradictions)
        try:
            self.state.document = await workflow.execute_activity(
                write_requirements_document,
                args=args,
                start_to_close_timeout=timedelta(minutes=6),
                schedule_to_close_timeout=timedelta(minutes=13),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("Requirements document failed: %s", exc.cause or exc)
            self.state.status = "interviewing"
            self.state.error = "The document could not be generated. Try again."
            return False
        workflow.logger.info("Requirements document written")
        self.state.status = "completed"
        return True

    async def _requirements_text(self) -> str:
        """The text of the session's requirements document."""
        document = self.state.document
        assert document is not None  # delivered only once the document was written
        try:
            return await workflow.execute_activity(
                read_requirements_document,
                args=[workflow.info().workflow_id, document["document_id"]],
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # The stored document expired; the requirement tables still carry the content.
            workflow.logger.warning("Requirements document unavailable: %s", exc.cause or exc)
            return render_requirements_markdown(self.state.requirements,
                                                retired=self.state.retired)

    async def _hand_off(self) -> RequirementsResult:
        """What this stage hands to its task flow as it closes: whether the user generated
        the document, and the start of the next stage it leads to: the architecture of a
        new service or an API change, or the delivery of a bugfix or documentation task.
        A document written because the session was idle leads to none."""
        finalized = self._finalized and self.state.status == "completed"
        if not finalized:
            self._review_skipped()
        result = RequirementsResult(finalized=finalized, kind=self.state.kind,
                                    requirements=list(self.state.requirements),
                                    document=self.state.document,
                                    reviewed=finalized and self._review_reported())
        if finalized and self.state.repository:
            if self.state.kind in DIRECT_KINDS:
                result.delivery = await self._delivery_state()
            elif self.state.kind in ARCHITECTURE_KINDS:
                session_id = (self.state.next_session_id
                              or f"architecture-session-{workflow.uuid4().hex}")
                architecture = architecture_state(workflow.info().workflow_id, self.state,
                                                  await self._requirements_text())
                architecture.task_flow_id = self.state.task_flow_id
                if architecture.design is not None:
                    architecture.design.task_flow_id = self.state.task_flow_id
                result.architecture = architecture
                self.state.architecture_session_id = session_id
        await self._close_flags()
        return result

    async def _deliver(self) -> None:
        """Hand the brief and the repository's contract to a delivery and stay open
        until that ends."""
        await self._run_delivery(await self._delivery_state())

    async def _delivery_state(self) -> DeliveryState:
        """The delivery of a task that leaves the API as it is: the brief and the
        repository's contract."""
        session_id = workflow.info().workflow_id
        document = await self._requirements_text()
        contract = self.state.contract
        assert contract is not None  # a task on an existing service read its contract
        return DeliveryState(spec=TaskSpec(
            session_id=session_id,
            requirements=contract["requirements"],
            requirements_document=contract["requirements_document"],
            api=contract["api"],
            kind=self.state.kind,
            brief={"requirements": self.state.requirements,
                   "requirements_document": document,
                   "retired": self.state.retired},
            language=contract["language"],
            repository=self.state.repository,
            checkout=self.state.checkout,
            # Given to the coding agents read-only; the repository's files stay as they are.
            architecture=contract.get("architecture"),
            architecture_structure=contract.get("architecture_structure"),
        ))

    async def _expire(self) -> None:
        """After a day without messages, keep whatever was gathered."""
        workflow.logger.info("Requirements session idle, expiring")
        self._expired = True
        # A document written because the session was idle is not reviewed.
        self._review_skipped()
        complete = bool(self.state.kind and self.state.requirements)
        if complete and await self._write_document():
            return
        self.state.status = "failed"
        self.state.error = (
            # Why it could not, when the repository is the reason.
            self.state.error or "The session expired and the document could not be generated."
            if complete
            else "The session expired before the kind of task and any requirements "
                 "were recorded."
        )


@workflow.defn
class ApiDesignWorkflow(Interview[ApiDesignState], _Delivering, FlowFlags, PendingStage,
                        OpenStage):
    """One API design discussion based on a finished requirements session: the
    model drafts the API first, or for a change to an existing service applies the
    change to the API its repository holds, each user message is an update, and the
    finalize update renders the API model as an OpenAPI document, which the
    session then hands to a DeliveryWorkflow."""

    INTERVIEWER = "designer"
    TOO_LONG = DISCUSSION_TOO_LONG

    @workflow.init
    def __init__(self, state: ApiDesignState) -> None:
        self.state = state
        # A new session starts with the draft; one forked after a user message
        # starts by answering it.
        self._started_unanswered = _ends_with_user_message(self.state)
        if not self.state.messages or self._started_unanswered:
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        self._turn_lock = asyncio.Lock()
        self._init_delivery()
        # Set when the session expired after a day without messages.
        self._expired = False
        self._init_flags()
        self._init_pending()
        self._init_open_stage()

    def _flags(self) -> tuple[bool | None, bool | None]:
        return _interview_flags(self.state)

    @workflow.run
    async def run(self, state: ApiDesignState) -> Any:
        if self.state.pending:
            # Created by its task flow before the architecture was finalized or skipped.
            await self._wait_pending()
            return await self._stay_open(None)
        if self.state.task_flow_id:
            self._keep_flags()
        ready = (await _check_out(self.state, self._resume_status)
                 and await self._confirm_contract())
        if ready and not self.state.messages:
            async with self._turn_lock:
                # A change has no questions to fall back on, so its opening stays
                # failed until it is retried.
                turned = await self._turn()
                if not turned and self.state.change_document:
                    workflow.deprecate_patch("retryable-change-opening")
                if not turned and not self.state.change_document:
                    self.state.messages.append(opening_fallback())
                    self.state.error = None
        elif ready and _opens_with_answer(self._started_unanswered, self.state):
            async with self._turn_lock:
                await self._turn()
        while self.state.status not in {"completed", "failed"}:
            seen = self._updates
            try:
                await workflow.wait_condition(
                    lambda: self._updates != seen
                    or self.state.status in {"completed", "failed"}
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=SESSION_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self.state.status == "interviewing":
                    await self._expire()
                continue
            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.state.task_flow_id:
            # A stage of a task flow hands its delivery to the task flow, which starts it
            # as its own child.
            result = DesignResult(document=self.state.document)
            if self.state.status == "completed" and self.state.document:
                result.delivery = self._delivery_state()
            await self._close_flags()
            return await self._stay_open(result)
        if self.state.status == "completed" and self.state.document:
            await self._deliver()
            if self._delivery_cancelled:
                await end_cancelled(FLOW_CANCELLED)
        elif (self.state.status == "failed" and workflow.patched(FLOW_ABANDONED_PATCH)
              and not expiry_ends_with_status(self._expired)):
            # The design could not start before anything was delivered: it was
            # abandoned, and shows as cancelled, as does one that expired, with the
            # reason in its state.
            await end_cancelled(FLOW_ABANDONED)
        return self.state.document

    @workflow.query
    def get_state(self) -> ApiDesignState:
        return self.state

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this session's stopped delivery again from `state`."""
        self._restart_delivery(state)

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        self._validate_restart_delivery()

    async def _confirm_contract(self) -> bool:
        """A change was specified against the contract of one commit, and this session
        cloned the repository again. When the branch has moved since, the change can go
        on only if the contract is still the same; False when it cannot."""
        commit = self.state.checkout["commit"] if self.state.checkout else None
        if not self.state.contract or commit == self.state.contract_commit:
            return True
        workflow.logger.info("Repository moved from %s to %s; comparing contracts",
                             self.state.contract_commit, commit)
        status = self.state.status
        self.state.status = "preparing"
        try:
            contract = await workflow.execute_activity(
                read_repository_contract,
                workflow.info().workflow_id,
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Repository contract unavailable: %s", exc.cause or exc)
            contract = None
        if contract != self.state.contract:
            self.state.status = "failed"
            self.state.error = (
                "The repository changed after this task's requirements were gathered, and "
                "its contract is no longer the one they were written against. Start a new "
                "requirements session for the change.")
            return False
        self.state.contract_commit = commit
        self.state.status = status
        return True

    def _check_open(self) -> None:
        self._check_opened()
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or (self.state.messages and self.state.messages[-1]["role"] != "user"):
                raise ApplicationError("There is no unanswered message to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or (self.state.messages and self.state.messages[-1]["role"] != "user"):
            raise ValueError("There is no unanswered message to retry.")

    @workflow.update
    async def finalize(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting changes.")
            workflow.logger.info("Finalize requested")
            await self._write_document()

    @finalize.validator
    def validate_finalize(self) -> None:
        self._check_opened()
        if self.state.status != "interviewing" or (self.state.messages and self.state.messages[-1]["role"] == "user"):
            raise ValueError("Retry the unanswered message before generating the OpenAPI.")
        problems = design_problems(self.state.api, self.state.language)
        if problems:
            raise ValueError("Fix these problems first: " + " ".join(problems))

    def _check_opened(self) -> None:
        if self.state.status == "interviewing" and not self.state.messages:
            raise ValueError("The session's opening failed. Retry it first.")

    async def _turn(self) -> bool:
        self.state.status = "turn_running"
        self.state.error = None
        try:
            result = await workflow.execute_activity(
                api_design_turn,
                self.state,
                start_to_close_timeout=timedelta(minutes=8),
                schedule_to_close_timeout=timedelta(minutes=9),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as exc:
            workflow.logger.warning("API design turn failed: %s", exc.cause or exc)
            self.state.error = "The LLM could not answer. Retry this message."
            ok = False
        else:
            self.state.messages.append(assistant_message(result))
            self.state.api = result["api"]
            self.state.language = result["language"]
            ok = True
        self.state.status = "interviewing"
        return ok

    async def _write_document(self) -> bool:
        self.state.status = "finalizing"
        self.state.error = None
        try:
            self.state.document = await workflow.execute_activity(
                write_openapi_document,
                args=[workflow.info().workflow_id, self.state.api],
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("OpenAPI document failed: %s", exc.cause or exc)
            self.state.status = "interviewing"
            self.state.error = "The OpenAPI document could not be generated. Try again."
            return False
        workflow.logger.info("OpenAPI document written")
        self.state.status = "completed"
        return True

    async def _deliver(self) -> None:
        """Hand the finished design to its delivery and stay open until that ends."""
        await self._run_delivery(self._delivery_state())

    def _delivery_state(self) -> DeliveryState:
        document = self.state.document
        assert document is not None  # delivered only once the document was written
        return DeliveryState(spec=TaskSpec(
            session_id=workflow.info().workflow_id,
            requirements=self.state.requirements,
            requirements_document=self.state.requirements_document,
            api=self.state.api,
            document_id=document["document_id"],
            kind=self.state.kind,
            language=self.state.language,
            repository=self.state.repository,
            checkout=self.state.checkout,
            architecture=self.state.architecture,
            architecture_structure=self.state.architecture_structure,
        ))

    async def _expire(self) -> None:
        """After a day without messages, keep the API if it is complete."""
        workflow.logger.info("API design session idle, expiring")
        self._expired = True
        # A failed opening has changed nothing, so there is no design to keep.
        if self.state.messages and not api_problems(self.state.api) and await self._write_document():
            return
        self.state.status = "failed"
        self.state.error = "The session expired before the API was complete."
