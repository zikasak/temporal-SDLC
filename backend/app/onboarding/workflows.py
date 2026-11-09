import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import empty_api
    from app.architecture.models import normalize_structure, render_architecture_markdown
    from app.delivery.models import DeliveryState, TaskSpec
    from app.llm.questions import answers_text
    from app.onboarding.activities import (
        find_onboarding_artifacts, inspect_onboarding_contract, onboarding_turn,
    )
    from app.onboarding.models import (
        ARCHITECTURE_PRESENT, DECISIONS, FINISHED_STATUSES, OnboardingState,
        draft_kind_changed, onboarding_problems,
    )
    from app.requirements.document import render_requirements_markdown
    from app.temporal.activities import checkout_repository, update_repository_checkout
    from app.temporal.closing import end_cancelled
    from app.temporal.workflows import (
        FLOW_ABANDONED, FLOW_ABANDONED_PATCH, FLOW_CANCELLED, SESSION_IDLE_TIMEOUT, _Delivering, assistant_message, check_answers, check_message,
        pending_questions,
    )

REVIEW_TOO_LONG = "This review is too long to continue. Finalize the onboarding."
CHECKOUT_FAILED = ("The repository could not be checked out. Check the GitHub connection "
                   "and start a new onboarding.")
CHECKOUT_UPDATE_FAILED = ("The repository could not be checked out again, so the earlier "
                          "checkout is kept. Check the GitHub connection and try again.")
ARTIFACTS_FAILED = "The repository's existing files could not be read. Retry."
ARTIFACTS_UPDATE_FAILED = ("The checkout was updated, but its existing contract, contract "
                           "tests and Dockerfile could not be read. Update the checkout "
                           "again before finalizing.")
DRAFT_FAILED = "The LLM could not draft the contract. Retry."
TURN_FAILED = "The LLM could not answer. Retry this message."
CONTRACT_UNKNOWN = ("The existing contract could not be read, so only replacing it or "
                    "declining is offered. Update the checkout to read it again.")


@workflow.defn
class OnboardingWorkflow(_Delivering):
    """One onboarding of an existing repository: the branch is checked out, the user
    confirms replacing a contract, contract tests or Dockerfile it already has, the LLM
    drafts the requirements, the API and the language from the code, and the user reviews
    the drafts in an interview of updates. Finalizing fixes the drafts as the contract and
    hands it to a DeliveryWorkflow, which writes and verifies the contract tests and the
    Dockerfile against the existing code, reviews and publishes them; the onboarding stays
    open as long as its delivery is."""

    @workflow.init
    def __init__(self, state: OnboardingState) -> None:
        self.state = state
        # A new onboarding or a fork checks out first; a continued run goes on as it was.
        if not self.state.checkout:
            self.state.status = "preparing"
        self._updates = 0
        self._turn_lock = asyncio.Lock()
        self._init_delivery()
        # Set when the onboarding expired without activity.
        self._expired = False

    @workflow.run
    async def run(self, state: OnboardingState) -> dict[str, Any] | None:
        if not self.state.checkout:
            await self._check_out()
            if self.state.status == "preparing":
                await self._after_checkout(self.state.existing_artifacts)
        while self.state.status not in FINISHED_STATUSES:
            seen = self._updates
            try:
                await workflow.wait_condition(
                    lambda: self._updates != seen
                    or self.state.status in FINISHED_STATUSES
                    or workflow.info().is_continue_as_new_suggested(),
                    timeout=SESSION_IDLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if self.state.status in ("interviewing", "awaiting_confirmation"):
                    self.state.status = "failed"
                    self.state.error = ("The onboarding expired after a day without "
                                        "activity. Start a new one.")
                    self._expired = True
                continue
            if workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.logger.info("Continuing as new")
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.state.status == "completed":
            await self._deliver()
            if self._delivery_cancelled:
                # Its pull request was closed without merging: the onboarding is cancelled.
                await end_cancelled(FLOW_CANCELLED)
        elif workflow.patched(FLOW_ABANDONED_PATCH):
            # The onboarding ended without running its delivery: it expired, failed,
            # was declined or replaced by a fork. It was abandoned, and shows as
            # cancelled.
            await end_cancelled(FLOW_ABANDONED)
        return None

    async def _check_out(self) -> None:
        try:
            self.state.checkout = await workflow.execute_activity(
                checkout_repository,
                args=[workflow.info().workflow_id, self.state.repository],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Repository checkout failed: %s", exc.cause or exc)
            self.state.status = "failed"
            self.state.error = CHECKOUT_FAILED

    async def _artifacts(self) -> list[str] | None:
        try:
            return await workflow.execute_activity(
                find_onboarding_artifacts, workflow.info().workflow_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Existing artifacts unknown: %s", exc.cause or exc)
            return None

    async def _after_checkout(self, known: list[str]) -> None:
        """Find what the branch has: what was not `known` before needs the user's decision;
        otherwise the onboarding goes on with drafting or the interview."""
        found = await self._artifacts()
        if found is None:
            self.state.status = "failed"
            self.state.error = ARTIFACTS_FAILED
            return
        self.state.existing_artifacts = found
        await self._inspect(found)
        accepted = known if self._decided() else []
        # A fork carries architecture_only over: adding only the architecture still needs
        # a contract without one that is readable apart from it, as in update_checkout.
        invalid = self._architecture_only_invalid()
        if invalid:
            workflow.deprecate_patch("onboarding-fork-architecture-check")
        if invalid or any(item not in accepted for item in found):
            self.state.replaced_contract = False
            self.state.architecture_only = False
            self.state.status = "awaiting_confirmation"
            workflow.logger.info("The branch already has %s; awaiting the user", found)
            return
        await self._proceed()

    def _architecture_only_invalid(self) -> bool:
        """Whether adding only the architecture was chosen but no longer applies: the
        branch has an architecture now, or the rest of its contract is unreadable."""
        return self.state.architecture_only and (
            not self.state.architecture_missing or bool(self.state.contract_problem))

    def _decided(self) -> bool:
        """Whether the user chose to go on with what the branch has."""
        return self.state.replaced_contract or self.state.architecture_only

    async def _inspect(self, found: list[str]) -> None:
        """Find whether the branch's contract lacks its architecture, which offers adding
        only that, and whether the rest of it is readable."""
        self.state.architecture_missing = False
        self.state.contract_problem = None
        if "contract" not in found:
            return
        workflow.deprecate_patch("onboarding-architecture")
        try:
            inspected = await workflow.execute_activity(
                inspect_onboarding_contract, workflow.info().workflow_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning("Existing contract unknown: %s", exc.cause or exc)
            self.state.architecture_missing = True
            self.state.contract_problem = CONTRACT_UNKNOWN
            return
        self.state.architecture_missing = bool(inspected["architecture_missing"])
        self.state.contract_problem = inspected.get("contract_problem")

    async def _proceed(self) -> None:
        """Draft the contract, or go on with the interview of the drafts."""
        async with self._turn_lock:
            if self._ended():
                return
            self._discard_drafts_of_other_kind(self.state.architecture_only)
            if not self.state.drafted:
                await self._draft()
            elif self.state.messages and self.state.messages[-1]["role"] == "user":
                await self._turn()
            else:
                self.state.status = "interviewing"

    async def _draft(self) -> bool:
        self.state.status = "drafting"
        self.state.error = None
        try:
            result = await self._execute_turn()
        except ActivityError as exc:
            workflow.logger.warning("Drafting failed: %s", exc.cause or exc)
            if self._ended():
                return False
            self.state.error = DRAFT_FAILED
            self.state.status = "interviewing"
            return False
        if self._ended():
            return False
        self._apply(result)
        self.state.drafted = True
        self.state.drafted_architecture_only = self.state.architecture_only
        self.state.status = "interviewing"
        workflow.logger.info("Contract drafted: %d requirements, %d endpoints, architecture %s",
                             len(self.state.requirements), len(self.state.api["endpoints"]),
                             "drafted" if self.state.architecture_structure else "missing")
        return True

    async def _turn(self) -> bool:
        self.state.status = "turn_running"
        self.state.error = None
        try:
            result = await self._execute_turn()
        except ActivityError as exc:
            workflow.logger.warning("Onboarding turn failed: %s", exc.cause or exc)
            if self._ended():
                return False
            self.state.error = TURN_FAILED
            self.state.status = "interviewing"
            return False
        if self._ended():
            return False
        self._apply(result)
        self.state.status = "interviewing"
        return True

    def _ended(self) -> bool:
        """Whether the onboarding ended, such as superseded by a fork, while a handler
        waited; its status then stays as it is."""
        return self.state.status in FINISHED_STATUSES

    async def _execute_turn(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            onboarding_turn,
            self.state,
            start_to_close_timeout=timedelta(minutes=12),
            schedule_to_close_timeout=timedelta(minutes=13),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    def _apply(self, result: dict[str, Any]) -> None:
        self.state.messages.append(assistant_message(result))
        self.state.requirements = result["requirements"]
        self.state.api = result["api"]
        self.state.language = result["language"]
        self.state.architecture_structure = result.get("architecture_structure")
        self.state.conflicts = list(result.get("conflicts") or [])

    @workflow.query
    def get_state(self) -> OnboardingState:
        return self.state

    def _check_open(self) -> None:
        if self.state.status != "interviewing" or not self.state.drafted:
            raise ValueError("The onboarding is not waiting for a message.")
        if self.state.messages and self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before sending another.")

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This onboarding is no longer accepting messages.")
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        self._check_open()
        if pending_questions(self.state.messages):
            raise ValueError("Answer the interviewer's questions first.")
        check_message(self.state.messages, message.strip(), REVIEW_TOO_LONG)

    @workflow.update
    async def answer_questions(self, answers: list[str]) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError("This onboarding is no longer accepting messages.")
            questions = pending_questions(self.state.messages)
            self.state.messages.append(
                {"role": "user", "content": answers_text(questions, answers)})
            await self._turn()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        self._check_open()
        check_answers(self.state.messages, answers, REVIEW_TOO_LONG)

    def _retryable(self) -> bool:
        return self.state.status == "interviewing" and (
            not self.state.drafted
            or bool(self.state.messages) and self.state.messages[-1]["role"] == "user")

    @workflow.update
    async def retry_message(self) -> None:
        """Run the failed drafting or interviewer turn again."""
        self._updates += 1
        async with self._turn_lock:
            if not self._retryable():
                raise ApplicationError("There is no failed turn to retry.")
            if not self.state.drafted:
                await self._draft()
            else:
                await self._turn()

    @retry_message.validator
    def validate_retry_message(self) -> None:
        if not self._retryable():
            raise ValueError("There is no failed drafting or turn to retry.")

    @staticmethod
    def _decision(decision: Any) -> str:
        # Histories from before the architecture_only choice sent a boolean.
        if isinstance(decision, bool):
            return "replace" if decision else "decline"
        return str(decision)

    def _discard_drafts_of_other_kind(self, architecture_only: bool) -> None:
        """Drafts of the other kind than `architecture_only` asks for do not carry over:
        the onboarding drafts anew. The kind is the one recorded when drafting, not the
        current decision, which a return to awaiting_confirmation resets."""
        if not self.state.drafted:
            return
        changed = draft_kind_changed(self.state, architecture_only)
        if changed != (architecture_only != self.state.architecture_only):
            workflow.deprecate_patch("onboarding-draft-kind")
        if not changed:
            return
        workflow.logger.info("Discarding the %s drafts to draft anew",
                             "architecture-only" if not architecture_only else "full")
        self.state.drafted = False
        self.state.drafted_architecture_only = None
        self.state.requirements = []
        self.state.api = empty_api()
        self.state.language = None
        self.state.architecture_structure = None
        self.state.conflicts = []

    @workflow.update
    async def contract_decision(self, decision: Any) -> None:
        """The user's choice about what the branch already has: replace it, decline the
        onboarding, or add only the architecture to the existing contract."""
        self._updates += 1
        choice = self._decision(decision)
        if self.state.status != "awaiting_confirmation":
            raise ApplicationError("The onboarding is not awaiting a decision.")
        if choice == "decline":
            workflow.logger.info("The user kept the existing contract; onboarding declined")
            self.state.status = "declined"
            self.state.error = None
            return
        architecture_only = choice == "architecture_only"
        self._discard_drafts_of_other_kind(architecture_only)
        if architecture_only:
            workflow.logger.info("The user chose to add only the architecture")
            self.state.requirements = []
            self.state.api = empty_api()
            self.state.language = None
        else:
            workflow.logger.info("The user chose to replace %s", self.state.existing_artifacts)
        self.state.architecture_only = architecture_only
        self.state.replaced_contract = not architecture_only
        await self._proceed()

    @contract_decision.validator
    def validate_contract_decision(self, decision: Any) -> None:
        if self.state.status != "awaiting_confirmation":
            raise ValueError("The onboarding is not awaiting a decision about the existing "
                             "contract.")
        choice = self._decision(decision)
        if choice not in DECISIONS:
            raise ValueError("The decision must be replace, decline or architecture_only.")
        if choice == "architecture_only":
            if not self.state.architecture_missing:
                raise ValueError(ARCHITECTURE_PRESENT)
            if self.state.contract_problem:
                raise ValueError(self.state.contract_problem)

    @workflow.update
    async def update_checkout(self) -> None:
        """Check the branch out again at its latest commit; the drafts stay as they are."""
        self._updates += 1
        async with self._turn_lock:
            status = self.state.status
            if status not in ("interviewing", "awaiting_confirmation"):
                raise ApplicationError("This onboarding is no longer accepting changes.")
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
                workflow.logger.warning("Repository checkout update failed: %s",
                                        exc.cause or exc)
                if self._ended():
                    return
                self.state.error = CHECKOUT_UPDATE_FAILED
                self.state.status = status
                return
            workflow.logger.info("Checked out again at %s", self.state.checkout["commit"])
            known = list(self.state.existing_artifacts)
            found = await self._artifacts()
            if self._ended():
                return
            if found is None:
                # The new commit may have added a contract, contract tests or a Dockerfile:
                # finalizing waits until a later update reads them and asks about new ones.
                self.state.artifacts_unknown = True
                self.state.error = ARTIFACTS_UPDATE_FAILED
                self.state.status = status
                return
            self.state.artifacts_unknown = False
            self.state.existing_artifacts = found
            await self._inspect(found)
            if self._ended():
                return
            accepted = known if self._decided() else []
            newly = [item for item in found if item not in accepted]
            # Adding only the architecture needs a contract without one, readable apart.
            invalid = self._architecture_only_invalid()
            if newly or invalid or (status == "awaiting_confirmation" and found):
                workflow.logger.info("The branch now has %s; awaiting the user", newly)
                self.state.replaced_contract = False
                self.state.architecture_only = False
                self.state.status = "awaiting_confirmation"
                return
            # Drafts of another kind than the decision asks for are drafted anew.
            self._discard_drafts_of_other_kind(self.state.architecture_only)
            self.state.status = "interviewing"
        if not self.state.drafted and status == "awaiting_confirmation":
            # Nothing is left to decide about, so drafting starts.
            await self._proceed()

    @update_checkout.validator
    def validate_update_checkout(self) -> None:
        if not self.state.checkout:
            raise ValueError("This onboarding has no checkout to update yet.")
        if self.state.status == "awaiting_confirmation":
            return
        if self.state.status != "interviewing" or not self.state.drafted and not self.state.error:
            raise ValueError("Wait for the running turn or drafting to finish.")
        if self.state.messages and self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before updating the checkout.")

    @workflow.update
    async def finalize(self) -> None:
        """Fix the drafts as the contract; the delivery starts once the run sees it."""
        self._updates += 1
        async with self._turn_lock:
            problems = onboarding_problems(self.state)
            if self.state.status != "interviewing" or problems:
                raise ApplicationError("The onboarding cannot be finalized now.")
            self.state.status = "finalizing"
            self.state.error = None
            workflow.logger.info("Onboarding finalized")
            self.state.status = "completed"

    @finalize.validator
    def validate_finalize(self) -> None:
        if self.state.status != "interviewing":
            raise ValueError("Wait for the running turn to finish before finalizing.")
        if self.state.messages and self.state.messages[-1]["role"] == "user":
            raise ValueError("Retry the unanswered message before finalizing.")
        problems = onboarding_problems(self.state)
        if problems:
            raise ValueError("Fix these problems first: " + " ".join(problems))

    @workflow.update
    def supersede(self) -> None:
        """End the onboarding for the fork that replaces it."""
        self._updates += 1
        self.state.status = "superseded"

    @supersede.validator
    def validate_supersede(self) -> None:
        if self.state.status in FINISHED_STATUSES:
            raise ValueError("Only an unfinished onboarding can be forked.")
        # A running turn, drafting or checkout update would write its status over it.
        if (self.state.status not in ("interviewing", "awaiting_confirmation")
                or self._turn_lock.locked()):
            raise ValueError("Wait for the running turn, drafting or checkout update to "
                             "finish before forking.")

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this onboarding's stopped delivery again from `state`."""
        self._restart_delivery(state)

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        self._validate_restart_delivery()

    def _architecture(self) -> tuple[str | None, dict[str, Any] | None]:
        if not self.state.architecture_structure:
            return None, None
        structure = normalize_structure(self.state.architecture_structure)
        return render_architecture_markdown(structure), dict(structure)

    async def _deliver(self) -> None:
        """Hand the contract to the delivery and stay open until that ends."""
        architecture, structure = self._architecture()
        if self.state.architecture_only:
            # Only contracts/architecture.md and contracts/architecture.json are pushed.
            await self._run_delivery(DeliveryState(spec=TaskSpec(
                session_id=workflow.info().workflow_id,
                requirements=[],
                requirements_document="",
                api=empty_api(),
                repository=self.state.repository,
                checkout=self.state.checkout,
                onboarding=True,
                architecture=architecture,
                architecture_structure=structure,
                architecture_only=True,
            )))
            return
        await self._run_delivery(DeliveryState(spec=TaskSpec(
            session_id=workflow.info().workflow_id,
            requirements=self.state.requirements,
            requirements_document=render_requirements_markdown(self.state.requirements),
            api=self.state.api,
            language=self.state.language,
            repository=self.state.repository,
            checkout=self.state.checkout,
            onboarding=True,
            architecture=architecture,
            architecture_structure=structure,
        )))
