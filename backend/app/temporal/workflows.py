import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.api_design.openapi import api_problems, design_problems
    from app.api_design.prompt import opening_fallback
    from app.delivery.models import DeliveryState, TaskSpec, delivery_id
    from app.github.models import RepositorySelection
    from app.requirements.document import render_requirements_markdown
    from app.requirements.models import (
        CHANGE_KINDS, CONTRACT_KINDS, DIRECT_KINDS, KIND_PROBLEM, RequirementsState,
        numbered_after,
    )
    from app.llm.questions import answer_problem, answers_text
    from app.requirements.prompt import opening_message
    from app.temporal.activities import (
        api_design_turn,
        checkout_repository,
        greet,
        parse_document,
        read_repository_contract,
        read_requirements_document,
        requirements_turn,
        send_llm_message,
        update_repository_checkout,
        write_openapi_document,
        write_requirements_document,
    )

SESSION_IDLE_TIMEOUT = timedelta(hours=24)
# How long a Chat message may wait for its session's repository.
REPOSITORY_CHECKOUT_TIMEOUT = timedelta(minutes=10)
MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 10000
OPEN_STATUSES = ("interviewing", "turn_running")
INTERVIEW_TOO_LONG = "This interview is too long to continue. Generate the document."
DISCUSSION_TOO_LONG = "This discussion is too long to continue. Generate the OpenAPI."


def pending_questions(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The questions the last message asked with ask_user, which the next must answer."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    return messages[-1].get("questions") or []


def check_message(messages: list[dict[str, Any]], text: str, too_long: str) -> None:
    """Raise ValueError unless `text` can be the session's next user message."""
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"A message must contain 1 to {MAX_MESSAGE_CHARS} characters.")
    encoded = json.dumps(
        [*messages, {"role": "user", "content": text}],
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
        raise ValueError(too_long)


def check_answers(messages: list[dict[str, Any]], answers: list[str], too_long: str) -> None:
    """Raise ValueError unless `answers` answer the questions the session waits for."""
    questions = pending_questions(messages)
    if not questions:
        raise ValueError("There are no questions to answer.")
    problem = answer_problem(questions, answers)
    if problem:
        raise ValueError(problem)
    check_message(messages, answers_text(questions, answers), too_long)


def assistant_message(result: dict[str, Any]) -> dict[str, Any]:
    """The message a turn added: its reply and the questions it asked."""
    message = {"role": "assistant", "content": result["reply"]}
    if result.get("questions"):
        message["questions"] = result["questions"]
    return message


def _await_checkout(state: RequirementsState | ApiDesignState) -> str:
    """Hold a new session in `preparing` until its repository is checked out, so no
    message reaches the model first. Returns the status to continue with."""
    status = state.status
    if state.repository and not state.checkout and status in OPEN_STATUSES:
        state.status = "preparing"
    return status


async def _check_out(state: RequirementsState | ApiDesignState, resume_status: str) -> bool:
    """Check out the repository of a `preparing` session; False when it cannot go on."""
    if state.status != "preparing":
        return True
    # Histories from before checkouts existed replay without one.
    if workflow.patched("repository-checkout"):
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
    state.status = resume_status
    return True


@workflow.defn
class SayHelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet,
            name,
            schedule_to_close_timeout=timedelta(seconds=10),
        )


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
            args=[messages, session_id, repository],
            start_to_close_timeout=timedelta(minutes=6),
            schedule_to_close_timeout=timedelta(minutes=7),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class RequirementsWorkflow:
    """One requirements interview: each user message is an update, and the
    finalize update turns the gathered requirements into a document. A task that
    leaves the API as it is has no API design to follow, so the session then hands
    it to a DeliveryWorkflow itself."""

    @workflow.init
    def __init__(self, state: RequirementsState | None = None) -> None:
        self.state = state or RequirementsState(messages=[opening_message()])
        # A session forked after a user message starts by answering it.
        if self.state.messages[-1]["role"] == "user":
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        # Validators can all pass in one workflow task, so turns queue here.
        self._turn_lock = asyncio.Lock()

    @workflow.run
    async def run(self, state: RequirementsState | None = None) -> dict[str, Any] | None:
        ready = await _check_out(self.state, self._resume_status)
        if ready and self.state.messages[-1]["role"] == "user":
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
        if self.state.status == "completed" and self.state.kind in DIRECT_KINDS:
            await self._deliver()
        return self.state.document

    @workflow.query
    def get_state(self) -> RequirementsState:
        return self.state

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            workflow.logger.info("Message received (%d characters)", len(message))
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        if pending_questions(self.state.messages):
            raise ValueError("Answer the analyst's questions first.")
        check_message(self.state.messages, message.strip(), INTERVIEW_TOO_LONG)

    @workflow.update
    async def answer_questions(self, answers: list[str]) -> None:
        """The user's answers to the questions of the last message, as their message."""
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            questions = pending_questions(self.state.messages)
            workflow.logger.info("Answers received (%d questions)", len(questions))
            self.state.messages.append(
                {"role": "user", "content": answers_text(questions, answers)})
            await self._turn()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        check_answers(self.state.messages, answers, INTERVIEW_TOO_LONG)

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
                raise ApplicationError("There is no unanswered message to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] != "user":
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
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before generating the document.")
        if not self.state.kind:
            raise ValueError(KIND_PROBLEM)
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
            self.state.contract = await workflow.execute_activity(
                read_repository_contract,
                workflow.info().workflow_id,
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Repository contract unavailable: %s", exc.cause or exc)
            unsupported = isinstance(exc.cause, ApplicationError) and exc.cause.type == "ContractError"
            self.state.error = (exc.cause.message if unsupported
                                else "The repository's contract could not be read. Try again.")
            return False
        self.state.contract_commit = self.state.checkout["commit"]
        if self.state.kind not in CHANGE_KINDS:
            # A documentation task leaves the contract as it is.
            return True
        # The task's requirements join the contract's when it is published. The interview
        # numbers them past the contract's already; this holds whatever it was given.
        self.state.requirements = numbered_after(
            self.state.requirements,
            [item["id"] for item in self.state.contract["requirements"]])
        # Only what this contract requires can be ended, and as the contract states it.
        ended = {item["id"] for item in self.state.retired}
        self.state.retired = [item for item in self.state.contract["requirements"]
                              if item["id"] in ended]
        return True

    async def _write_document(self) -> bool:
        self.state.status = "finalizing"
        self.state.error = None
        # Only a task on an existing service keeps a contract, whatever kind it was before,
        # and only a change to that service ends requirements of it.
        self.state.contract = None
        if self.state.kind not in CHANGE_KINDS:
            self.state.retired = []
        if self.state.kind in CONTRACT_KINDS and not await self._read_contract():
            self.state.status = "interviewing"
            return False
        try:
            self.state.document = await workflow.execute_activity(
                write_requirements_document,
                args=[workflow.info().workflow_id, self.state.messages, self.state.requirements,
                      self.state.retired],
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

    async def _deliver(self) -> None:
        """Hand the brief and the repository's contract to a delivery and stay open
        until that ends."""
        session_id = workflow.info().workflow_id
        try:
            document = await workflow.execute_activity(
                read_requirements_document,
                args=[session_id, self.state.document["document_id"]],
                start_to_close_timeout=timedelta(minutes=1),
                schedule_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # The stored document expired; the requirement tables still carry the content.
            workflow.logger.warning("Requirements document unavailable: %s", exc.cause or exc)
            document = render_requirements_markdown(self.state.requirements,
                                                    retired=self.state.retired)
        contract = self.state.contract
        try:
            await workflow.execute_child_workflow(
                "DeliveryWorkflow",
                DeliveryState(spec=TaskSpec(
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
                )),
                id=delivery_id(session_id),
            )
        except ChildWorkflowError as exc:
            # Stage failures are results of the delivery; only an aborted one ends here.
            workflow.logger.warning("Delivery did not finish: %s", exc.cause or exc)

    async def _expire(self) -> None:
        """After a day without messages, keep whatever was gathered."""
        workflow.logger.info("Requirements session idle, expiring")
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
class ApiDesignWorkflow:
    """One API design discussion based on a finished requirements session: the
    model drafts the API first, or for a change to an existing service applies the
    change to the API its repository holds, each user message is an update, and the
    finalize update renders the API model as an OpenAPI document, which the
    session then hands to a DeliveryWorkflow."""

    @workflow.init
    def __init__(self, state: ApiDesignState) -> None:
        self.state = state
        # A new session starts with the draft; one forked after a user message
        # starts by answering it.
        if not self.state.messages or self.state.messages[-1]["role"] == "user":
            self.state.status = "turn_running"
        self._resume_status = _await_checkout(self.state)
        self._updates = 0
        self._turn_lock = asyncio.Lock()

    @workflow.run
    async def run(self, state: ApiDesignState) -> dict[str, Any] | None:
        ready = (await _check_out(self.state, self._resume_status)
                 and await self._confirm_contract())
        if ready and not self.state.messages:
            async with self._turn_lock:
                if not await self._turn():
                    self.state.messages.append(
                        opening_fallback(bool(self.state.change_document)))
                    self.state.error = None
        elif ready and self.state.messages[-1]["role"] == "user":
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
        if self.state.status == "completed" and self.state.document:
            await self._deliver()
        return self.state.document

    @workflow.query
    def get_state(self) -> ApiDesignState:
        return self.state

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

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            workflow.logger.info("Message received (%d characters)", len(message))
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        if pending_questions(self.state.messages):
            raise ValueError("Answer the designer's questions first.")
        check_message(self.state.messages, message.strip(), DISCUSSION_TOO_LONG)

    @workflow.update
    async def answer_questions(self, answers: list[str]) -> None:
        """The user's answers to the questions of the last message, as their message."""
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This session is no longer accepting messages.")
            questions = pending_questions(self.state.messages)
            workflow.logger.info("Answers received (%d questions)", len(questions))
            self.state.messages.append(
                {"role": "user", "content": answers_text(questions, answers)})
            await self._turn()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        if self.state.status != "interviewing" or self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")
        check_answers(self.state.messages, answers, DISCUSSION_TOO_LONG)

    @workflow.update
    async def retry_message(self) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing" or not self.state.messages or self.state.messages[-1]["role"] != "user":
                raise ApplicationError("There is no unanswered message to retry.")
            await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if self.state.status != "interviewing" or not self.state.messages or self.state.messages[-1]["role"] != "user":
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
        if self.state.status != "interviewing" or (self.state.messages and self.state.messages[-1]["role"] == "user"):
            raise ValueError("Retry the unanswered message before generating the OpenAPI.")
        problems = design_problems(self.state.api, self.state.language)
        if problems:
            raise ValueError("Fix these problems first: " + " ".join(problems))

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
        session_id = workflow.info().workflow_id
        try:
            await workflow.execute_child_workflow(
                "DeliveryWorkflow",
                DeliveryState(spec=TaskSpec(
                    session_id=session_id,
                    requirements=self.state.requirements,
                    requirements_document=self.state.requirements_document,
                    api=self.state.api,
                    document_id=self.state.document["document_id"],
                    kind=self.state.kind,
                    language=self.state.language,
                    repository=self.state.repository,
                    checkout=self.state.checkout,
                )),
                id=delivery_id(session_id),
            )
        except ChildWorkflowError as exc:
            # Stage failures are results of the delivery; only an aborted one ends here.
            workflow.logger.warning("Delivery did not finish: %s", exc.cause or exc)

    async def _expire(self) -> None:
        """After a day without messages, keep the API if it is complete."""
        workflow.logger.info("API design session idle, expiring")
        if not api_problems(self.state.api) and await self._write_document():
            return
        self.state.status = "failed"
        self.state.error = "The session expired before the API was complete."
