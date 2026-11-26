import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

from app.delivery.flow.base_merge import BaseMerge
from app.delivery.flow.baseline import Baseline
from app.delivery.flow.change import Change
from app.delivery.flow.ci import CiWatch
from app.delivery.flow.mention_rounds import MentionRounds
from app.delivery.flow.merge_conflicts import MergeConflicts
from app.delivery.flow.publication import Publication
from app.delivery.flow.review import ReviewRounds
from app.delivery.flow.stages import RESTORE_KEPT_PATCH, Stages, restore_finding
from app.delivery.flow.verification import Verification
from app.delivery.flow.waiting import PullRequestWait
from app.temporal.flags import FlowFlags, PendingStage

with workflow.unsafe.imports_passed_through():
    from app.delivery.causes import with_cause
    from app.config import (
        CODEGEN_TASK_QUEUE, DOCUMENTATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
    )
    from app.delivery.models import (
        DELIVERY_CLOSING, MAX_AGENT_ANSWERS, TASK_IDLE_TIMEOUT, AgentQuestions, ProviderWait,
        DeliveryProgress, DeliveryState, mention_key, open_review_findings,
    )
    from app.review.models import review_request
    from app.temporal.execution_fields import task_fields

# Marks the workflow code that waits for its pull request to be merged or closed,
# checking it every PULL_REQUEST_CHECK_INTERVAL; runs that began to wait before it only
# react to the pull request's webhook and to the workers' check signal.
PULL_REQUEST_CLOSES_PATCH = 'pull-request-closes'


def _same_mention(first: tuple[str | None, int | None],
                  second: tuple[str | None, int | None]) -> bool:
    """Whether two mention identities name one comment; an identity without a type, from
    a state recorded before types were, matches by comment ID alone."""
    return first[1] == second[1] and (
        first[0] is None or second[0] is None or first[0] == second[0])


@workflow.defn
class DeliveryWorkflow(Stages, PullRequestWait, CiWatch, MentionRounds, MergeConflicts,
                       BaseMerge, Change, Baseline, Verification, ReviewRounds, Publication,
                       FlowFlags, PendingStage):
    """Turns a specified task into verified, published sources: code generation, the
    parallel service and test authors, verification with its repair loop, and the push.
    A bugfix generates no code and goes red to green instead: regression tests, proof
    that they fail against the repository's service, then the fix and the same
    verification. The repository's tests are copied aside before any of this, and every
    later comparison is with that copy. A documentation task has one author, limited to
    documentation files, and no check yet: what it wrote is pushed marked as unverified.
    A stage that fails records its own error instead of failing the run, so it can be
    retried on its own. A finished run stays open, as its session does, for the user's
    next decision: the restart update continues it as a new run, so every retry stays a
    child of the session the task belongs to.

    The run and its pipelines are here; each stage's rounds are a mixin of
    app.delivery.flow, all working on the one `state`, `spec` and `progress`."""

    @workflow.init
    def __init__(self, state: DeliveryState) -> None:
        self.state = state
        self.spec = state.spec
        self.progress = state.progress
        # How often each child workflow ID was used in this run, to keep them unique.
        self._child_ids: dict[str, int] = {}
        # How many contract tests the repository has, as this run's record of them says.
        self._repository_tests = 0
        self._next: DeliveryState | None = None
        # The next review round when a mention's fix round started.
        self._mention_round = 0
        # Why the reply that the mention's fix round started could not be posted.
        self._ack_error: str | None = None
        # The comment whose fix round this run carries out, as review rounds refer to it.
        self._mention_ref: dict[str, Any] | None = None
        # The code review findings the authors are correcting now, which the analysis of
        # failed checks is told: empty while no correction runs.
        self._correcting: list[dict[str, Any]] = []
        # Whether this run's own push succeeded, whether or not its commit changed: a
        # round whose correction was already present pushes the same SHA again.
        self._pushed = False
        # An onboarding's question whether a failing test reveals a bug in the existing
        # code, and the user's answer to it.
        self._bug_question = False
        self._bug_answer: str | None = None
        # Why this run's fix round of an architecture-only onboarding changed nothing,
        # and the model's summary of the change it made.
        self._fix_error: str | None = None
        self._fix_summary: str | None = None
        # The pull request GitHub reported closed, by its number, and whether it was
        # merged; and whether its state is to be checked now.
        self._closed: tuple[int, bool] | None = None
        self._check_requested = False
        # Whether this run has been given something to do: a run that is not only a
        # recorded decision, or a mention handed to it.
        self._handed_over = not state.record_only
        # The failed CI this run fixes on its own, if it is such a round.
        self._ci_round: dict[str, Any] | None = state.ci_fix
        # The conflicting remote commits this run takes in on its own, if it is such a
        # round, and those this run's push met, which a round takes in next.
        self._merge_round: dict[str, Any] | None = state.merge_fix
        self._push_conflict: dict[str, Any] | None = None
        # Whether GitHub reported that the pull request may conflict with its base
        # branch, to be checked now; and whether this run said in the pull request that
        # no further base merge fix round starts on its own.
        self._conflict_requested = False
        self._base_merge_stopped = False
        self._init_flags()
        self._init_pending()

    def _flags(self) -> tuple[bool | None, bool | None]:
        """Waiting for the user while an agent asks questions or, once the run waits, open
        review findings wait for a decision; unresolved while those findings are open."""
        open_findings = bool(self.progress.open_findings)
        return (bool(self.progress.agent_questions) or (self.state.waiting and open_findings),
                open_findings)

    @workflow.run
    async def run(self, state: DeliveryState) -> DeliveryProgress:
        if self.state.pending:
            # Created by its task flow before the task was specified.
            await self._wait_pending()
            return self.progress
        self._keep_flags()
        # Mentions that ended before this run, such as in a stopped one, are kept in the
        # mention history and leave the workflow's state, once a fix round that was
        # stopped has had its outcome replied.
        await self._reply_stopped()
        await self._flush_mentions()
        # A decision of the user's, such as accepting open findings, is recorded as it
        # is: nothing runs and nothing is pushed.
        if not self.state.record_only:
            if self.state.mention is None:
                await self._deliver()
                if self._ci_round is not None:
                    await self._ci_fix_finished()
            elif await self._mention_started():
                await self._deliver()
                await self._mention_finished()
            if self._merge_round is not None:
                await self._merge_fix_finished()
        workflow.deprecate_patch('delivery-waits')
        self.state.waiting = True
        # A fix request that arrived while this run worked starts its round now.
        self._start_queued()
        # Then a round of remote commits this run's push conflicted with.
        await self._start_merge_fix()
        if workflow.patched(PULL_REQUEST_CLOSES_PATCH):
            if not await self._wait_for_pull_request():
                await self._close_flags()
                return self.progress
        else:
            # A run that began to wait before deliveries watched their pull request: the
            # pull request's webhook still closes it, and the signal the workers send
            # every waiting delivery when they start moves it on to a run that checks.
            while True:
                try:
                    await workflow.wait_condition(
                        lambda: (self._next is not None or self._closing() is not None
                                 or self._check_requested),
                        timeout=TASK_IDLE_TIMEOUT)
                    break
                except asyncio.TimeoutError:
                    workflow.logger.info('Delivery idle, closing')
                    self.state.waiting = False
                    await workflow.wait_condition(workflow.all_handlers_finished)
                    # A mention whose classification overlapped the idle timeout may have
                    # turned out to be a fix request: it was accepted, so the delivery
                    # waits again and starts its round instead of closing with it queued.
                    if not self._fix_pending():
                        await self._close_flags()
                        return self.progress
                    workflow.deprecate_patch('idle-queued-mentions')
                    workflow.logger.info('Fix request queued while closing; continuing')
                    self.state.waiting = True
                    self._start_queued()
            if self._next is None:
                merged = self._closing()
                if merged is not None:
                    return await self._close(merged)
                # Continued as a run that checks its pull request, changing nothing.
                self.state.waiting = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                self.state.record_only = True
                self.state.retry_stage = None
                self.state.instructions = None
                workflow.continue_as_new(self.state)
        await workflow.wait_condition(workflow.all_handlers_finished)
        following = self._next
        assert following is not None  # the wait above ends only with a next run
        if following is not self.state:
            # Mentions received since the user's decision was read stay with the delivery.
            following.progress.mentions = self.progress.mentions
            running = (following.mention_type, following.mention)
            following.mention_queue = [
                item for item in self.state.mention_queue
                if following.mention is None or not _same_mention(mention_key(item), running)]
        workflow.continue_as_new(following)

    @workflow.update
    def restart(self, state: DeliveryState) -> None:
        """Continue the finished delivery as a new run from `state`: the user's retry,
        acceptance of open findings or instructions about them."""
        state.waiting = False
        self.state.waiting = False
        self._next = state

    @restart.validator
    def validate_restart(self, state: DeliveryState) -> None:
        if not self.state.waiting:
            raise ValueError('Wait for the current generation to finish.')
        if self._closing() is not None:
            raise ValueError(DELIVERY_CLOSING)

    async def _deliver(self) -> None:
        if self.spec.architecture_only:
            # Only contracts/architecture.md and contracts/architecture.json are pushed,
            # without tests, verification or code review.
            self.progress.verification_status = 'not_applicable'
            if self.state.mention is not None and self.state.instructions:
                # A fix requested on the pull request changes the architecture first; a
                # change that could not be made leaves the earlier push as it is.
                if not await self._revise_architecture(self.state.instructions):
                    return
            if self.spec.checkout:
                self.progress.publication_status = 'waiting'
                self.progress.pull_request_status = 'waiting'
            await self._publish_architecture()
            return
        if not await self._snapshot():
            self.progress.verification_status = 'skipped'
        elif self.state.instructions or self.state.mention is not None:
            await self._instructed()
        elif self.state.retry_stage:
            await self._retry()
        else:
            if self.spec.checkout:
                self.progress.publication_status = 'waiting'
                self.progress.pull_request_status = 'waiting'
            if self.spec.onboarding:
                await self._onboard()
            elif self.spec.kind == 'bugfix':
                await self._change()
            elif self.spec.kind == 'docs':
                await self._document()
            else:
                await self._generate()
        await self._publish()

    @workflow.query
    def get_state(self) -> DeliveryState:
        return self.state

    @workflow.signal
    def agent_asked(self, asked: AgentQuestions) -> None:
        """A stage's agent paused for the user's answers, which go to its workflow."""
        self.progress.agent_questions = [
            *(item for item in self.progress.agent_questions
              if item['workflow_id'] != asked['workflow_id']), asked]

    @workflow.signal
    def agent_waiting(self, waiting: ProviderWait) -> None:
        """A stage's agent waits for its model provider's limits until `until`; a wait
        without `until` has ended."""
        self.progress.provider_waits = [
            *(item for item in self.progress.provider_waits
              if item['workflow_id'] != waiting['workflow_id']),
            *([waiting] if waiting.get('until') else [])]

    @workflow.signal
    def agent_answered(self, answered: Any) -> None:
        """A stage's agent was answered, or its questions timed out: by the stage's
        workflow ID alone, or with the questions and the user's answers, which the later
        agents of the delivery are told."""
        if isinstance(answered, dict):
            workflow_id = answered['workflow_id']
            self.state.agent_answers = [*self.state.agent_answers, {
                'stage': answered['stage'], 'questions': answered['questions'],
                'answers': answered['answers']}][-MAX_AGENT_ANSWERS:]
        else:
            workflow_id = answered
        self.progress.agent_questions = [item for item in self.progress.agent_questions
                                         if item['workflow_id'] != workflow_id]

    async def _snapshot(self) -> bool:
        """Record the repository's tests before any author runs; the activity keeps the
        first record, so a retried delivery compares with the same one."""
        # A documentation task neither reads nor writes a test, and an onboarding writes
        # its suite anew.
        if (not self.spec.checkout or not self.spec.language or self.spec.kind == 'docs'
                or self.spec.onboarding):
            return True
        try:
            recorded = await workflow.execute_activity(
                'snapshot_repository_tests', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository tests not recorded: %s', exc.cause or exc)
            error = with_cause("The repository's tests could not be recorded.", exc)
            # Reported on the stage whose retry starts the delivery over.
            if self.spec.kind == 'bugfix':
                self.progress.tests_error = error
            else:
                self.progress.code_error = error
            return False
        self._repository_tests = int(recorded.get('tests') or 0)
        return True

    async def _generate(self) -> None:
        """Generate code, join the authoring branches, then verify their images."""
        self.progress.verification_status = 'waiting'
        if not self.spec.language:
            self.progress.code_error = "No implementation language was chosen for this session."
            self.progress.verification_status = 'skipped'
            return
        try:
            self.progress.code = await workflow.execute_child_workflow(
                "CodegenWorkflow",
                args=[self.spec.session_id, self.spec.document_id, self.spec.language],
                id=self._child_id('codegen'),
                task_queue=CODEGEN_TASK_QUEUE,
                **task_fields(self.spec),
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Code generation failed: %s", exc.cause or exc)
            self.progress.code_error = with_cause("The server code could not be generated.", exc)
            self.progress.verification_status = 'skipped'
            return
        workflow.logger.info("Server code generated")
        # Each branch records its own failure, so one never cancels the other.
        await asyncio.gather(self._implement_service(), self._generate_tests())
        await self._verify_built()

    async def _onboard(self) -> None:
        """Onboard the existing code: its Dockerfile and the contract tests written apart,
        then verification against each other, as for a generated service."""
        self.progress.verification_status = 'waiting'
        if not self.spec.language:
            self.progress.implementation_error = 'No implementation language was chosen.'
            self.progress.verification_status = 'skipped'
            return
        await asyncio.gather(self._implement_service(), self._generate_tests())
        await self._verify_built()

    async def _document(self, instructions: str | None = None) -> None:
        """Change the repository's documentation: its author, then the check of what it
        wrote. Which check that is, and whether there is one yet, is verification's own
        matter; the result decides how the documentation is published. `instructions`
        are a fix requested in the pull request, made to the documentation as pushed."""
        self.progress.verification_status = 'waiting'
        context = self._stage_context(None)
        if instructions:
            context['fix_request'] = {'instructions': instructions}
            self.progress.documentation_error = None
            self.progress.verification_error = None
            self.progress.verification = None
        try:
            self.progress.documentation = await self._child(
                'DocumentationWorkflow', DOCUMENTATION_TASK_QUEUE,
                self._child_id('documentation'), context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('Documentation failed: %s', exc.cause or exc)
            self.progress.documentation_error = with_cause(
                'The documentation could not be written.', exc)
            self.progress.verification_status = 'skipped'
            return
        self.progress.verification_status = 'running'
        try:
            self.progress.verification = await workflow.execute_activity(
                'verify_documentation', self.spec.session_id,
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Documentation verification failed: %s', exc.cause or exc)
            self.progress.verification_error = with_cause(
                'Documentation verification could not complete.', exc)
            self.progress.verification_status = 'error'
        else:
            self.progress.verification_status = 'completed'
        if not self._documentation_failed():
            # The requested fix, if any, is written and checked: nothing is left to retry.
            self.state.documentation_fix = None

    def _documentation_failed(self) -> bool:
        """Whether the documentation stage failed, as its retry is offered: its author
        failed, or the check of what it wrote could not complete."""
        verification = self.progress.verification
        return bool(self.progress.documentation_error or self.progress.verification_error
                    or (verification and verification.get('status') == 'error'))

    async def _retry(self) -> None:
        if self.state.retry_stage == 'repository':
            # Generation and verification already succeeded; only the push is repeated.
            return
        if self.state.retry_stage == 'review':
            # The retry clears the flag at once, for the page; it says so by the status.
            if (self.progress.correction_failed
                    or self.progress.verification_status == 'repairing'):
                await self._retry_correction()
            else:
                # The verified delivery is reviewed again; a new run of review rounds,
                # which knows what the last completed round found and had corrected.
                await self._review_rounds(corrected=open_review_findings(self.progress))
            return
        if self.spec.kind == 'docs':
            # A failed fix requested in the pull request is retried as that fix.
            await self._document(self.state.documentation_fix)
            return
        if self.state.code_fix and self.state.retry_stage in ('server', 'tests'):
            # A failed fix from instructions or a mention is retried as that fix.
            await self._retry_fix()
            return
        if self.spec.kind == 'bugfix':
            if self.state.retry_stage == 'tests':
                # New tests have to be shown to fail without the fix again.
                await self._change()
                return
        elif (self.state.retry_stage == 'server' and not self.progress.code
              and not self.spec.onboarding):
            # A failed codegen never started either authoring branch.
            await self._generate()
            return
        if self.state.retry_stage == 'server':
            await self._implement_service()
        elif self.state.restore_tests:
            workflow.deprecate_patch(RESTORE_KEPT_PATCH)
            # The suite was rejected for tests the changed service still passed: its
            # author is told so and puts them back, rather than writing the same again.
            restore, self.state.restore_tests = self.state.restore_tests, []
            await self._generate_tests(
                review_request([restore_finding(restore)], 'tests', 1, restore=restore))
        else:
            await self._generate_tests()
        await self._verify_built()
