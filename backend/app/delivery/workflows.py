import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.config import (
        CODEGEN_TASK_QUEUE, COMPONENT_TEST_GENERATION_TASK_QUEUE, DIAGNOSIS_TASK_QUEUE,
        DOCUMENTATION_TASK_QUEUE,
        IMPLEMENTATION_TASK_QUEUE, REVIEW_TASK_QUEUE, ROUTING_TASK_QUEUE,
        TEST_GENERATION_TASK_QUEUE, VERIFICATION_TASK_QUEUE,
    )
    from app.delivery.contract import with_requirements
    from app.delivery.models import (
        COMPONENT_CHANGE_UNPUBLISHED, COMPONENT_CHANGE_UNREPAIRED, MENTION_HANDLED,
        OUTCOME_OWED, STOPPED, TASK_IDLE_TIMEOUT, AgentQuestions, DeliveryProgress,
        DeliveryState, component_run, is_mention, judge_baseline, judge_kept, kept_tests,
        mention_key, open_review_findings, restorable_tests, unproven_green,
    )
    from app.diagnosis.models import repair_request
    from app.github.checkout import SNAPSHOT_DIR
    from app.github.models import PullRequestMention
    from app.temporal.execution_fields import task_fields
    from app.requirements.models import CHANGE_KINDS
    from app.routing.models import COMPONENT_TESTS_UNRUN, judge_routes
    from app.review.models import (
        MAX_REVIEW_ROUNDS, ReviewFinding, ReviewRound, earlier_findings, for_worker,
        review_request,
    )
    from app.temporal.activities import (
        open_pull_request, publish_documentation, publish_onboarding,
        publish_onboarding_architecture, publish_repository,
    )

# Each round is a diagnosis, a repair of one side and a new verification.
MAX_REPAIR_ROUNDS = 5
# Rounds in which the test author of an API change puts back the repository's tests the
# changed service still passes, each verified again.
MAX_RESTORE_ROUNDS = 2
# Marks the workflow code that sends a suite rejected for such tests back to its author.
RESTORE_KEPT_PATCH = 'restore-kept-tests'
# Marks the workflow code that writes component tests for criteria routed to a seam, which
# stopped the delivery at its routing before.
COMPONENT_TESTS_PATCH = 'component-tests'
# Marks the workflow code that runs component tests, at the baseline and with the change,
# where deliveries stopped once they were written before.
COMPONENT_RUNS_PATCH = 'component-runs'
# Failed tests a verification that is not repaired names.
NAMED_FAILURES = 5
# Runs of one verification whose result is an infrastructure error, not a verdict.
VERIFICATION_ATTEMPTS = 2
# An API change may add more tests than one run reports on singly; the run still
# answers for every test of the suite.
MAX_TRACKED = 1000
# How the LLM may classify a mention of the app.
MENTION_CLASSES = ('fix', 'clarification', 'unclear')
# Statuses of a mention that has ended; it then lives in the mention history alone.
MENTION_ENDED = ('replied', 'completed', 'error')
UNCLEAR_REPLY = ('I could not tell what you would like me to do. Mention me again and say '
                 'whether you are asking for a fix to the code of this pull request or for '
                 'an explanation of it.')
FIX_STARTED = ('Started a fix round from your comment: the changes, then verification and '
               'code review. I will reply here again with the outcome.')
# What an onboarding asks the user when the diagnosis finds the existing code at fault.
BUG_QUESTION = ('A contract test fails against the existing code. Does the failure reveal a '
                'bug in the existing code?')
BUG_CONFIRMED = 'Yes: it is a bug, change the existing code'
SETUP_AT_FAULT = 'No: the Dockerfile or the service setup is at fault, keep the code'
TESTS_AT_FAULT = 'No: the code is right, repair the tests and the contract'
BUG_SUMMARY_CHARS = 600
DOCS_FIX_STARTED = ('Started a fix round of the documentation from your comment: the '
                    'changes, then their check. I will reply here again with the outcome.')
ARCHITECTURE_FIX_STARTED = ('Started a fix round of the architecture from your comment: '
                            'contracts/architecture.md and contracts/architecture.json are '
                            'changed as it asks and pushed to this pull request, without '
                            'tests or code review. I will reply here again with the outcome.')


def _same_mention(first: tuple[str | None, int | None],
                  second: tuple[str | None, int | None]) -> bool:
    """Whether two mention identities name one comment; an identity without a type, from
    a state recorded before types were, matches by comment ID alone."""
    return first[1] == second[1] and (
        first[0] is None or second[0] is None or first[0] == second[0])


def restore_finding(kept: list[str],
                    reason: str = 'Verification rejected the tests.') -> ReviewFinding:
    """The finding that sends the test author back to the repository's `kept` tests it
    changed or removed although the changed service still passes them."""
    return {'summary': (
        'These tests of the repository were changed or removed, but the changed service '
        f'still passes them as the repository holds them: {", ".join(kept)}. A test the '
        'contract still supports has to stay exactly as the repository holds it: put '
        'each one back as the `restore` entry gives it.'),
        'target': 'tests', 'reason': reason}


@workflow.defn
class DeliveryWorkflow:
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
    child of the session the task belongs to."""

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

    @workflow.run
    async def run(self, state: DeliveryState) -> DeliveryProgress:
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
            elif await self._mention_started():
                await self._deliver()
                await self._mention_finished()
        # Histories from before deliveries waited end with their work.
        if not workflow.patched('delivery-waits'):
            return self.progress
        self.state.waiting = True
        # A fix request that arrived while this run worked starts its round now.
        self._start_queued()
        while True:
            try:
                await workflow.wait_condition(lambda: self._next is not None,
                                              timeout=TASK_IDLE_TIMEOUT)
                break
            except asyncio.TimeoutError:
                workflow.logger.info('Delivery idle, closing')
                self.state.waiting = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                # A mention whose classification overlapped the idle timeout may have
                # turned out to be a fix request: it was accepted, so the delivery waits
                # again and starts its round instead of closing with it queued.
                if not self._fix_pending() or not workflow.patched('idle-queued-mentions'):
                    return self.progress
                workflow.logger.info('Fix request queued while closing; continuing')
                self.state.waiting = True
                self._start_queued()
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self._next is not self.state:
            # Mentions received since the user's decision was read stay with the delivery.
            self._next.progress.mentions = self.progress.mentions
            running = (self._next.mention_type, self._next.mention)
            self._next.mention_queue = [
                item for item in self.state.mention_queue
                if self._next.mention is None or not _same_mention(mention_key(item), running)]
        workflow.continue_as_new(self._next)

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

    @workflow.update
    def answer_questions(self, answers: list[str]) -> None:
        """The user's answer to the onboarding's question whether a failing contract test
        reveals a bug in the existing code."""
        self._bug_answer = answers[0].strip()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        if not self._bug_question or self._bug_answer is not None:
            raise ValueError('These questions are no longer open.')
        if len(answers) != 1 or not answers[0].strip():
            raise ValueError('Answer the question with one answer.')

    @workflow.signal
    def agent_answered(self, workflow_id: str) -> None:
        self.progress.agent_questions = [item for item in self.progress.agent_questions
                                         if item['workflow_id'] != workflow_id]

    @workflow.update
    async def mention(self, mention: PullRequestMention) -> None:
        """An authorised mention of the app on the delivery's pull request: classified,
        then answered right away from the code as last pushed, or queued as a fix round
        that runs after the current one, in the order the requests arrived. Its full text
        is kept in the mention history, never in the workflow's state."""
        body = mention['body']
        entry: PullRequestMention = {
            **{key: value for key, value in mention.items() if key != 'body'},
            'classification': 'pending', 'status': 'classifying'}
        self.progress.mentions.append(entry)
        # Its place in the queue is taken as it arrives: a fix request whose
        # classification ends sooner never overtakes one that arrived before it.
        self.state.mention_queue.append([entry['comment_type'], entry['comment_id']])
        # Identifies this handover: retries of its recording activity share it, while
        # a separate handover of the same comment, as an update of its own, does not.
        update_info = workflow.current_update_info()
        recording = f"{workflow.info().run_id}:{update_info.id if update_info else ''}"
        try:
            added = await workflow.execute_activity(
                'record_new_mention',
                args=[self.spec.session_id, {**entry, 'body': body}, recording],
                result_type=bool, start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError as exc:
            workflow.logger.warning('Mention not recorded: %s', exc.cause or exc)
            await self._mention_failed(entry, 'The comment could not be recorded.')
            await self._settle(entry)
            self._start_queued()
            return
        if not added:
            # Handed over again after it was handled, such as by a recovered handover.
            self.progress.mentions = [item for item in self.progress.mentions
                                      if item is not entry]
            self.state.mention_queue = [item for item in self.state.mention_queue
                                        if not is_mention(entry, mention_key(item))]
            self._start_queued()
            raise ApplicationError(MENTION_HANDLED, non_retryable=True)
        await self._handle_mention(entry, body)
        if entry['status'] in MENTION_ENDED:
            await self._settle(entry)

    async def _handle_mention(self, entry: PullRequestMention, body: str) -> None:
        try:
            classification = await workflow.execute_activity(
                'classify_mention', args=[self.spec.session_id, body],
                result_type=str, start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Mention not classified: %s', exc.cause or exc)
            await self._mention_failed(entry, 'The comment could not be classified.')
            self._start_queued()
            return
        if classification not in MENTION_CLASSES:
            classification = 'unclear'
        entry['classification'] = classification
        workflow.logger.info('Mention %d classified as %s', entry['comment_id'], classification)
        if classification == 'fix':
            problem = self._fix_unavailable()
            if problem:
                await self._mention_failed(entry, problem)
                self._start_queued()
                return
            entry['status'] = 'queued'
            self._start_queued()
            return
        # Not a fix request: the requests queued behind it may go on.
        self._start_queued()
        if classification == 'clarification':
            try:
                text = await workflow.execute_activity(
                    'answer_mention',
                    args=[self.spec.session_id, body, self._mention_context()],
                    result_type=str, start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
            except ActivityError as exc:
                workflow.logger.warning('Mention not answered: %s', exc.cause or exc)
                await self._mention_failed(entry, 'The question could not be answered.')
                return
        else:
            text = UNCLEAR_REPLY
        if await self._reply(entry, text):
            entry['status'] = 'replied'

    @mention.validator
    def validate_mention(self, mention: PullRequestMention) -> None:
        if any(item['comment_id'] == mention['comment_id']
               and item['comment_type'] == mention['comment_type']
               for item in self.progress.mentions):
            raise ValueError(MENTION_HANDLED)

    def _fix_unavailable(self) -> str | None:
        """Why the delivery cannot take a fix round, if it cannot."""
        if self.spec.architecture_only and not workflow.patched('architecture-fixes'):
            # Histories from before architecture fixes turned them down.
            return ('This pull request only adds the architecture to the contract, which '
                    'was reviewed in the onboarding; start a new onboarding to change it.')
        if self.spec.architecture_only:
            # The architecture takes fixes without tests, verification or code review.
            if not self.progress.publication or not self.spec.checkout:
                return 'The architecture has not been pushed yet.'
            return None
        # A documentation delivery takes fixes of its documentation, without code review.
        if self.spec.kind != 'docs' and not self.spec.language:
            return 'No implementation language was chosen for this delivery.'
        if not self.progress.publication or not self.spec.checkout:
            return 'The delivery has not been pushed yet.'
        return None

    def _queued_entry(self, key: tuple[str | None, int | None]) -> PullRequestMention | None:
        """The fix request of the comment `key`, as (comment type, comment ID)."""
        if key[1] is None:
            return None
        return next((item for item in self.progress.mentions
                     if is_mention(item, key) and item['classification'] == 'fix'
                     and item['status'] in ('queued', 'running')), None)

    def _running_entry(self) -> PullRequestMention | None:
        return self._queued_entry((self.state.mention_type, self.state.mention))

    def _fix_pending(self) -> bool:
        """Whether an accepted fix request still waits in the queue for its round."""
        return any(self._queued_entry(mention_key(item)) is not None
                   for item in self.state.mention_queue)

    def _start_queued(self) -> None:
        """Start the round of the oldest queued fix request once the delivery waits. The
        queue holds every mention in the order it arrived; one that is still being
        classified holds back those behind it, and one that turned out not to be a fix
        request is passed over."""
        while self.state.waiting and self._next is None and self.state.mention_queue:
            key = mention_key(self.state.mention_queue[0])
            if any(is_mention(item, key) and item['classification'] == 'pending'
                   and item['status'] == 'classifying' for item in self.progress.mentions):
                return
            self.state.mention_queue.pop(0)
            entry = self._queued_entry(key)
            if entry is None:
                continue
            progress = self.progress
            progress.agent_questions = []
            progress.review_error = None
            progress.correction_failed = False
            progress.implementation_error = None
            progress.tests_error = None
            progress.verification_status = 'repairing'
            self.state.waiting = False
            self.state.retry_stage = None
            self.state.record_only = False
            # The comment's text is read from the mention history when its round starts.
            self.state.instructions = None
            self.state.mention = entry['comment_id']
            self.state.mention_type = entry['comment_type']
            self._next = self.state

    def _mention_context(self) -> dict[str, Any]:
        """What a reply to a question about the delivery is based on, besides its code."""
        brief = self.spec.brief
        return {
            'kind': self.spec.kind,
            'language': self.spec.language,
            'requirements': brief['requirements'] if brief else self.spec.requirements,
            'retired': brief['retired'] if brief else [],
            # The pushed commit the answer reads the code of.
            'repository': self.spec.repository,
            'publication': self.progress.publication,
            'verification_status': self.progress.verification_status,
            'reviews': self.progress.reviews[-3:],
            'open_findings': self.progress.open_findings,
        }

    async def _reply(self, entry: PullRequestMention, text: str) -> bool:
        """Reply to the mention in the pull request it was made on, even when the delivery
        links to another pull request since; false when that failed."""
        pull = self.progress.pull_request
        full_name = entry.get('repository') or (
            self.spec.repository['full_name'] if self.spec.repository else None)
        number = entry.get('pull_number') or (pull['number'] if pull else None)
        if not full_name or not number:
            entry['status'] = 'error'
            entry['error'] = 'The delivery has no pull request to reply in.'
            return False
        # An inline comment is answered in its thread, through the thread's first comment.
        reply_to = entry.get('reply_to') or entry['comment_id']
        try:
            entry['reply_url'] = await workflow.execute_activity(
                'reply_to_mention',
                args=[full_name, number, entry['comment_type'], reply_to, entry['author'],
                      text],
                result_type=str, start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Reply to mention failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            entry['status'] = 'error'
            entry['error'] = reason or 'The reply could not be posted in the pull request.'
            return False
        return True

    async def _mention_failed(self, entry: PullRequestMention, error: str) -> None:
        await self._reply(entry, f'Sorry, this could not be done: {error}')
        # What failed is the mention's error, whether or not the reply was posted.
        entry['status'] = 'error'
        entry['error'] = error

    async def _settle(self, entry: PullRequestMention) -> None:
        """Keep how the mention stands in the mention history; one that ended then leaves
        the workflow's state, which so stays bounded. One that could not be kept stays in
        the state, shown from there, and is kept by the next run."""
        try:
            await workflow.execute_activity(
                'save_mention', args=[self.spec.session_id, entry],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError as exc:
            workflow.logger.warning('Mention %d not saved: %s', entry['comment_id'],
                                    exc.cause or exc)
            return
        if entry['status'] in MENTION_ENDED:
            self.progress.mentions = [item for item in self.progress.mentions
                                      if item is not entry]

    async def _reply_stopped(self) -> None:
        """Reply the outcome of the fix rounds stopped from outside, such as by the
        delivery being terminated, whose reviewers were told that they started."""
        for entry in [item for item in self.progress.mentions if item.get(OUTCOME_OWED)]:
            replied = await self._reply(
                entry, 'The fix round was stopped before it finished, so nothing was '
                       'pushed. The branch keeps its earlier push. Mention me again to '
                       'request the fix once more.')
            entry.pop(OUTCOME_OWED, None)
            entry['status'] = 'error'
            entry['error'] = STOPPED + ('' if replied else (
                ' The reply with this outcome could not be posted in the pull request: '
                f'{entry.get("error") or "not posted"}.'))

    async def _flush_mentions(self) -> None:
        for entry in [item for item in self.progress.mentions
                      if item['status'] in MENTION_ENDED]:
            await self._settle(entry)

    async def _mention_started(self) -> bool:
        """Read the text of the comment whose fix round this run carries out and reply
        that the round started; false when the round cannot start. A failed reply does
        not stop the round, but is kept and reported when it ends."""
        entry = self._running_entry()
        self._mention_round = len(self.progress.reviews) + 1
        self._ack_error = None
        self._pushed = False
        if entry is None:
            self.state.mention = None
            self.state.mention_type = None
            self.state.instructions = None
            return False
        try:
            self.state.instructions = await workflow.execute_activity(
                'mention_body',
                args=[self.spec.session_id, entry['comment_type'], entry['comment_id']],
                result_type=str, start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError as exc:
            workflow.logger.warning('Mention text not read: %s', exc.cause or exc)
            self.state.mention = None
            self.state.mention_type = None
            self.state.instructions = None
            await self._mention_failed(entry, 'The comment could not be read.')
            await self._settle(entry)
            return False
        self._mention_ref = {'comment_type': entry['comment_type'],
                             'comment_id': entry['comment_id']}
        entry['status'] = 'running'
        started = (ARCHITECTURE_FIX_STARTED if self.spec.architecture_only
                   else DOCS_FIX_STARTED if self.spec.kind == 'docs' else FIX_STARTED)
        if not await self._reply(entry, started):
            self._ack_error = entry.get('error') or 'The reply could not be posted.'
            # The round goes on and shows what failed meanwhile.
            entry['status'] = 'running'
            entry['error'] = ('The reply that the fix round started could not be posted '
                              f'in the pull request: {self._ack_error}')
        return True

    async def _mention_finished(self) -> None:
        entry = self._running_entry()
        instructions = self.state.instructions
        self.state.mention = None
        self.state.mention_type = None
        self.state.instructions = None
        self._mention_ref = None
        if (self.spec.kind == 'docs' and instructions
                and self._documentation_failed()):
            # The failed fix stays retryable as the same task: retrying the documentation
            # stage hands these instructions to its author again.
            self.state.documentation_fix = instructions
        # A failed code fix keeps its instructions, findings and comment in code_fix,
        # which retrying its server or tests stage carries out again.
        ack_error, self._ack_error = self._ack_error, None
        if entry is None:
            return
        if len(self.progress.reviews) >= self._mention_round:
            entry['round'] = self._mention_round
        publication = self.progress.publication
        # Pushed by this round, even with an unchanged SHA; an earlier publication the
        # branch keeps after a failed round does not count.
        pushed = bool(self._pushed and self.progress.publication_status == 'completed'
                      and publication)
        fix_error, self._fix_error = self._fix_error, None
        fix_summary, self._fix_summary = self._fix_summary, None
        if pushed:
            if self.spec.architecture_only:
                verified = ('Only the architecture changed, so no tests, verification or '
                            'code review ran.'
                            + (f'\n\n{fix_summary}' if fix_summary else ''))
            elif self.spec.kind == 'docs':
                verified = ('It passed its check.' if publication['verified']
                            else 'No check exists for documentation yet, so it is marked '
                                 'as unverified.')
            elif publication['verified']:
                verified = 'It passed verification and code review.'
            else:
                verified = (f'The tests pass, but {publication.get("findings_count") or 0} '
                            'code review findings remain, so it is marked as unverified.')
            pushed_to = (f'The fix round ended and its result was pushed to '
                         f'`{publication["branch"]}` at commit '
                         f'`{publication["commit"][:12]}`.')
            replied = await self._reply(entry, f'{pushed_to} {verified}')
            failed = []
            if ack_error:
                failed.append(f'the reply that it started ({ack_error})')
            if not replied:
                failed.append('the reply with its outcome '
                              f'({entry.get("error") or "not posted"})')
            if failed:
                # The push stands, but a reply the mention is owed never reached GitHub.
                entry['status'] = 'error'
                entry['error'] = (f'{pushed_to} These replies could not be posted in the '
                                  f'pull request: {"; ".join(failed)}.')
            else:
                entry['status'] = 'completed'
                entry.pop('error', None)
            await self._settle(entry)
            return
        error = (fix_error or self.progress.documentation_error
                 or self.progress.implementation_error or self.progress.tests_error
                 or self.progress.baseline_error or self.progress.verification_error
                 or self.progress.review_error or self.progress.publication_error
                 or 'The fix round did not pass verification, so nothing was pushed.')
        replied = await self._reply(entry, f'The fix round ended without a push: {error} '
                                           'The branch keeps its earlier push.')
        entry['status'] = 'error'
        failed = [text for text, missed in (
            (f'the reply that it started ({ack_error})', bool(ack_error)),
            (f'the reply with its outcome ({entry.get("error") or "not posted"})',
             not replied)) if missed]
        entry['error'] = error + (
            f' These replies could not be posted in the pull request: {"; ".join(failed)}.'
            if failed else '')
        await self._settle(entry)


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
            error = "The repository's tests could not be recorded."
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
            self.progress.code_error = "The server code could not be generated."
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

    async def _change(self) -> None:
        """Change the repository's service red to green: the suite that can prove each
        criterion, regression tests, proof that they fail against the service as it is,
        then the fix and the whole suite."""
        self.progress.verification_status = 'waiting'
        self.progress.baseline_status = 'waiting'
        # No test is written for criteria no suite can observe.
        if not await self._route():
            self.progress.baseline_status = 'skipped'
            self.progress.verification_status = 'skipped'
            return
        await self._generate_tests()
        # A failed author, or one that found a criterion its suite cannot observe, leaves
        # nothing to hold against the service, whatever the other author wrote.
        if self.progress.component_tests_error or self.progress.routing_status != 'routed':
            self.progress.baseline_status = 'skipped'
            self.progress.verification_status = 'skipped'
            return
        if self.progress.component_tests and not workflow.patched(COMPONENT_RUNS_PATCH):
            # Deliveries that wrote component tests before they could run them stopped here.
            self.progress.baseline_status = 'skipped'
            self.progress.baseline_error = COMPONENT_TESTS_UNRUN
            self.progress.verification_status = 'skipped'
            return
        # Nothing is fixed before the added tests are shown to fail without the fix.
        if not await self._reproduce():
            self.progress.verification_status = 'skipped'
            return
        # Retried tests are verified against the fix the delivery already has.
        if not self.progress.implementation:
            await self._implement_service()
        await self._verify_built()

    async def _route(self) -> bool:
        """Decide which suite can prove each criterion of the brief: true when every one
        has one. A routing that found one for each is kept for the retries of the tests."""
        if self.progress.routing_status == 'routed':
            return True
        self.progress.routing_status = 'running'
        self.progress.routing = None
        self.progress.routing_error = None
        try:
            result = await workflow.execute_child_workflow(
                'ObservabilityWorkflow',
                args=[self.spec.session_id, self._stage_context(None)],
                id=self._child_id('routing'),
                task_queue=ROUTING_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Routing failed: %s', exc.cause or exc)
            self.progress.routing_error = ('It could not be decided which tests can prove the '
                                           'acceptance criteria.')
            self.progress.routing_status = 'error'
            return False
        self.progress.routing = {
            requirement: {**route, 'source': 'routing'}
            for requirement, route in result['routes'].items()}
        self.progress.routing_status, self.progress.routing_error = self._judge_routes(
            self.progress.routing)
        workflow.logger.info('Routing: %s', self.progress.routing_status)
        return self.progress.routing_status == 'routed'

    def _judge_routes(self, routing: dict[str, Any]) -> tuple[str, str | None]:
        component = any(item['route'] == 'component' for item in routing.values())
        # Asked only when it decides something, so other deliveries' histories stay as
        # they were.
        return judge_routes(routing, component_tests=(
            component and workflow.patched(COMPONENT_TESTS_PATCH)))

    def _component_routes(self) -> dict[str, Any]:
        """The criteria routed to a seam, with their reasons and seams."""
        return {requirement: route for requirement, route in (self.progress.routing or {}).items()
                if route['route'] == 'component'}

    def _declined(self, unobservable: list[dict[str, str]]) -> None:
        """Record criteria a test author found its suite cannot observe."""
        routing = dict(self.progress.routing or {})
        for item in unobservable:
            routing[item['requirement_id']] = {
                'route': 'unobservable', 'reason': item['reason'], 'source': 'tests'}
        self.progress.routing = routing
        self.progress.routing_status, self.progress.routing_error = self._judge_routes(routing)
        workflow.logger.info('The test author found %d criteria unobservable',
                             len(unobservable))

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
            self.progress.documentation = await workflow.execute_child_workflow(
                'DocumentationWorkflow',
                args=[self.spec.session_id, context],
                id=self._child_id('documentation'),
                task_queue=DOCUMENTATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Documentation failed: %s', exc.cause or exc)
            self.progress.documentation_error = 'The documentation could not be written.'
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
            self.progress.verification_error = 'Documentation verification could not complete.'
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

    async def _reproduce(self, repair: int = 0) -> bool:
        """Run the changed suite against the service as the repository holds it: true
        when every added test fails there and every existing test passes, but for
        those a repair corrected. A change with component tests runs them too, and is
        judged on both suites."""
        if self.progress.component_tests:
            return await self._reproduce_components(repair)
        if not self.progress.tests or self.progress.tests_error:
            self.progress.baseline_status = 'skipped'
            return False
        self.progress.baseline_status = 'running'
        self.progress.baseline = None
        self.progress.baseline_error = None
        added, changed = (self.progress.tests[key] for key in ('added_tests', 'changed_tests'))
        try:
            image = await self._build_baseline()
            self.progress.baseline = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[self.spec.session_id, image['image_id'], self.progress.tests['image_id'],
                      self.spec.language, [*added, *changed]],
                id=self._child_id('baseline', repair),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Baseline run failed: %s', exc.cause or exc)
            self.progress.baseline_error = ('The tests could not be run against the service as '
                                            'the repository holds it.')
            self.progress.baseline_status = 'error'
            return False
        self.progress.baseline_status, self.progress.baseline_error = judge_baseline(
            self.progress.baseline, added, changed)
        workflow.logger.info('Baseline run: %s', self.progress.baseline_status)
        return self.progress.baseline_status == 'reproduced'

    async def _build_baseline(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            'build_baseline', args=[self.spec.session_id, self.spec.language],
            task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _contract_suite(self) -> tuple[str, str, list[str], list[str]] | None:
        """The contract tests a change with component tests runs: its author's suite, with
        the tests it added and the corrected ones, or, when the change gave it no
        criterion, the repository's own suite as it is, which the change has to keep
        passing. None when the repository has no contract tests either."""
        tests = self.progress.tests
        if tests:
            return tests['image_id'], 'tests', tests['added_tests'], tests['changed_tests']
        if not self._repository_tests:
            return None
        image = await workflow.execute_activity(
            'build_repository_tests', args=[self.spec.session_id, self.spec.language],
            task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return image['image_id'], SNAPSHOT_DIR, [], []

    async def _run_components(self, runner_image: str) -> dict[str, Any]:
        """Run the component tests once, and once more when the run could not complete,
        as a run of the contract tests is."""
        tests = self.progress.component_tests
        for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
            result = await workflow.execute_activity(
                'run_component_suite',
                args=[self.spec.session_id, runner_image, self.spec.language,
                      [*tests['added_tests'], *tests['changed_tests']][:MAX_TRACKED]],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if result['status'] != 'error':
                break
            workflow.logger.warning('Component test run %d could not complete: %s', attempt,
                                    result['error'])
        return result

    async def _reproduce_components(self, repair: int) -> bool:
        """The baseline of a change with component tests: its contract tests, if it has
        any, against the service as the repository holds it, and its component tests,
        built by their author with the code as the repository holds it."""
        if self.progress.tests_error or self.progress.component_tests_error:
            self.progress.baseline_status = 'skipped'
            return False
        self.progress.baseline_status = 'running'
        self.progress.baseline = None
        self.progress.baseline_error = None
        self.progress.component_baseline = None
        components = self.progress.component_tests
        added, changed = [], []
        try:
            suite = await self._contract_suite()
            if suite:
                test_image, suite_dir, added, changed = suite
                image = await self._build_baseline()
                self.progress.baseline = await workflow.execute_child_workflow(
                    'VerificationWorkflow',
                    args=[self.spec.session_id, image['image_id'], test_image,
                          self.spec.language, [*added, *changed], suite_dir],
                    id=self._child_id('baseline', repair),
                    task_queue=VERIFICATION_TASK_QUEUE,
                    result_type=dict,
                )
            self.progress.component_baseline = await self._run_components(
                components['image_id'])
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Baseline run failed: %s', exc.cause or exc)
            self.progress.baseline_error = ('The tests could not be run against the code as '
                                            'the repository holds it.')
            self.progress.baseline_status = 'error'
            return False
        self.progress.baseline_status, self.progress.baseline_error = judge_baseline(
            self.progress.baseline, added, changed, component_run(
                self.progress.component_baseline, components['added_tests'],
                components['changed_tests']))
        workflow.logger.info('Baseline run: %s', self.progress.baseline_status)
        return self.progress.baseline_status == 'reproduced'

    def _child_id(self, stage: str, repair: int = 0) -> str:
        child_id = f'{stage}-{self.spec.session_id}'
        if self.state.retry_stage or self.state.instructions or self.state.mention is not None:
            child_id += f'-{workflow.info().run_id}'
        if repair:
            child_id += f'-repair-{repair}'
        count = self._child_ids.get(child_id, 0) + 1
        self._child_ids[child_id] = count
        # Review rounds verify again, with repair rounds of their own.
        return child_id if count == 1 else f'{child_id}-{count}'

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
        elif self.state.restore_tests and workflow.patched(RESTORE_KEPT_PATCH):
            # The suite was rejected for tests the changed service still passed: its
            # author is told so and puts them back, rather than writing the same again.
            restore, self.state.restore_tests = self.state.restore_tests, []
            await self._generate_tests(
                review_request([restore_finding(restore)], 'tests', 1, restore=restore))
        else:
            await self._generate_tests()
        await self._verify_built()

    async def _verify_built(self) -> None:
        """Verification needs both images; a failed build leaves nothing to verify."""
        if self.progress.component_tests:
            await self._verify_components()
            return
        if (self.progress.implementation and self.progress.tests
                and not self.progress.implementation_error and not self.progress.tests_error):
            await self._verify_and_repair()
            if self.spec.kind == 'api_change':
                await self._prove_kept()
                if workflow.patched(RESTORE_KEPT_PATCH):
                    await self._restore_kept()
            if self._passed():
                await self._review_rounds()
        else:
            self.progress.verification_status = 'skipped'

    def _passed(self) -> bool:
        """Whether verification showed the implementation and the tests passing against
        each other, with everything a change needs proved."""
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'passed'
                    and not self.progress.verification_error
                    and not self.progress.implementation_error
                    and not self.progress.tests_error
                    and (self.spec.kind != 'bugfix'
                         or self.progress.baseline_status == 'reproduced'))

    def _components_passed(self) -> bool:
        """Whether verification showed a change with component tests passing both suites,
        after both were red without it."""
        contract, component = self.progress.verification, self.progress.component_verification
        return bool(self.progress.verification_status == 'completed'
                    and (contract is None or contract['status'] == 'passed')
                    and component and component['status'] == 'passed'
                    and not self.progress.verification_error
                    and not self.progress.implementation_error
                    and not self.progress.tests_error
                    and not self.progress.component_tests_error
                    and self.progress.baseline_status == 'reproduced')

    async def _verify_components(self) -> None:
        """Verify a change with component tests: the changed code must still conform to its
        component contract, and then pass its contract tests, if it has any, and its
        component tests, built with it. The change is not repaired, reviewed or pushed
        yet, whatever the outcome."""
        if (not self.progress.implementation or self.progress.implementation_error
                or self.progress.tests_error or self.progress.component_tests_error):
            self.progress.verification_status = 'skipped'
            return
        self.progress.verification_status = 'running'
        self.progress.verification = None
        self.progress.verification_error = None
        self.progress.component_verification = None
        service_image = self.progress.implementation['image_id']
        try:
            suite = await self._contract_suite()
            if suite:
                test_image, suite_dir, added, _ = suite
                for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
                    self.progress.verification = await workflow.execute_child_workflow(
                        'VerificationWorkflow',
                        args=[self.spec.session_id, service_image, test_image,
                              self.spec.language, added[:MAX_TRACKED], suite_dir],
                        id=self._child_id('verification'),
                        task_queue=VERIFICATION_TASK_QUEUE,
                        result_type=dict,
                    )
                    if self.progress.verification['status'] != 'error':
                        break
            runner = await workflow.execute_activity(
                'build_component_runner', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if 'conformance_error' not in runner:
                self.progress.component_verification = await self._run_components(
                    runner['image_id'])
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Verification of the change failed: %s', exc.cause or exc)
            self.progress.verification_error = 'Service verification could not complete.'
            self.progress.verification_status = 'error'
            return
        self.progress.verification_status = 'completed'
        self.progress.verification_error = self._judge_components(runner)
        workflow.logger.info('Verification of the change: %s',
                             'rejected' if self.progress.verification_error else 'passed')

    def _judge_components(self, runner: dict[str, Any]) -> str | None:
        """Why verification of a change with component tests did not show it working;
        None when it did."""
        contract, component = self.progress.verification, self.progress.component_verification
        if 'conformance_error' in runner:
            # The check says what differs; the fix has to keep the seam as it is.
            return (f'{runner["conformance_error"]} The fix has to keep every signature of '
                    f'the component contract. {COMPONENT_CHANGE_UNREPAIRED}')
        failed = []
        for name, run in (('contract tests', contract), ('component tests', component)):
            if run is None:
                continue
            if run['status'] == 'error':
                return (f'The {name} could not be run against the changed code: '
                        f'{run.get("error") or "the run did not complete"}.')
            if run['status'] == 'failed':
                named = ', '.join(f'{item["class_name"]}#{item["name"]}'
                                  for item in run['test_failures'][:NAMED_FAILURES])
                failed.append(f'{name} {run["failures"] + run["errors"]} of {run["tests"]}'
                              + (f' ({named})' if named else ''))
        if failed:
            return (f'Tests fail against the changed code: {"; ".join(failed)}. '
                    f'{COMPONENT_CHANGE_UNREPAIRED}')
        return unproven_green(contract, component)

    async def _review_rounds(self, instructions: str | None = None,
                             corrected: list[ReviewFinding] | None = None) -> None:
        """Review the verified delivery's changes; while new findings remain, send them to
        the worker they concern, verify again and review again, at most MAX_REVIEW_ROUNDS
        times. Each round knows the findings already `corrected` in this run, and a
        finding reported again is not sent for another correction: a round with nothing
        new ends the run. Findings left are open: the delivery is pushed as unverified
        and the user decides about them."""
        earlier = [{key: value for key, value in item.items() if key != 'repeats'}
                   for item in corrected or []]
        for count in range(1, MAX_REVIEW_ROUNDS + 1):
            # The user's instructions hold for every round of the run they started.
            findings = await self._review(instructions, earlier, record=count == 1)
            if findings is None:
                return
            if not findings:
                self.progress.verification_status = 'completed'
                return
            new = [item for item in findings if 'repeats' not in item]
            if not new:
                workflow.logger.info('The review only repeats findings already corrected')
                break
            if count == MAX_REVIEW_ROUNDS:
                workflow.logger.info('Review findings remain after %d rounds',
                                     MAX_REVIEW_ROUNDS)
                break
            if not await self._correct(new, self.progress.reviews[-1]['round']):
                return
            earlier += new
        self.progress.verification_status = 'findings_open'

    async def _review(self, instructions: str | None, earlier: list[ReviewFinding],
                      record: bool = True) -> list[ReviewFinding] | None:
        """One review round, told the findings already corrected; None when the review
        could not run. `record` keeps the instructions with the round whose reviewed
        fixes were made from them."""
        self.progress.verification_status = 'reviewing'
        number = len(self.progress.reviews) + 1
        context = self._coding_context()
        if instructions:
            context['instructions'] = instructions
        if earlier:
            context['previous_findings'] = earlier_findings(earlier)
        try:
            result = await workflow.execute_child_workflow(
                'ReviewWorkflow', args=[self.spec.session_id, context],
                id=self._child_id('review', number),
                task_queue=REVIEW_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Code review failed: %s', exc.cause or exc)
            self.progress.review_error = 'The code review could not run.'
            self.progress.verification_status = 'error'
            return None
        self.progress.review_error = None
        findings: list[ReviewFinding] = list(result.get('findings') or [])
        round_: ReviewRound = {'round': number, 'findings': findings}
        if instructions and record and self._mention_ref is not None:
            # A comment's text, up to GitHub's 65536 characters per comment, stays in the
            # mention history; the round refers to it so that the state stays bounded.
            round_['mention'] = dict(self._mention_ref)
        elif instructions and record:
            round_['instructions'] = instructions
        self.progress.reviews.append(round_)
        workflow.logger.info('Review round %d: %d findings', number, len(findings))
        return findings

    async def _correct(self, findings: list[ReviewFinding], number: int,
                       instructions: str | None = None,
                       restore: list[str] | None = None) -> bool:
        """Have each worker the findings concern correct them, or with the user's
        instructions both, then verify again. True when verification passed again.
        A correction of the review's own findings that did not pass is left for the user
        to retry; one from instructions leaves the open findings to decide about again."""
        self.progress.verification_status = 'repairing'
        workers = [worker for worker in ('implementation', 'tests')
                   if instructions or for_worker(findings, worker)]
        await asyncio.gather(*(
            (self._implement_service if worker == 'implementation' else self._generate_tests)(
                review_request(findings, worker, number, instructions, restore))
            for worker in workers))
        if await self._verify_correction(workers, number):
            return True
        self.progress.correction_failed = not instructions
        return False

    async def _retry_correction(self) -> None:
        """Go on from a correction of review findings that did not pass, then with the
        review rounds. A worker that failed, or corrected tests of a bugfix that no longer
        failed without the fix, correct the findings again. A suite that verification ran
        and rejected goes back to the test author with the reason, as the same suite would
        be rejected again. Otherwise the corrected delivery is only verified again."""
        self.progress.correction_failed = False
        number = self.progress.reviews[-1]['round']
        rejected = self._rejected_suite() if workflow.patched('correct-rejected-suite') else None
        if (self.progress.implementation_error or self.progress.tests_error
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            self.progress.implementation_error = None
            self.progress.tests_error = None
            corrected = await self._correct(open_review_findings(self.progress), number)
        elif rejected:
            finding, restore = rejected
            corrected = await self._correct([finding], number, restore=restore)
        else:
            self.progress.verification_status = 'repairing'
            corrected = await self._verify_correction([], number)
            self.progress.correction_failed = not corrected
        if corrected:
            await self._review_rounds(corrected=open_review_findings(self.progress))

    def _rejected_suite(self) -> tuple[ReviewFinding, list[str]] | None:
        """Why the last verification, which ran and passed, still rejected the suite of a
        change, as a finding for the test author, with the repository's tests it has to
        put back as they were: those it changed or removed that the changed service
        still passes. None when the verification proved nothing about the suite."""
        result = self.progress.verification
        if (self.spec.kind not in CHANGE_KINDS or not result
                or result['status'] != 'passed' or not self.progress.tests):
            return None
        reason = 'Verification of the corrected delivery rejected its tests.'
        unrun = unproven_green(result)
        if unrun:
            return {'summary': unrun, 'target': 'tests', 'reason': reason}, []
        existing = self.progress.existing_tests
        if self.spec.kind != 'api_change' or not existing:
            return None
        tests = self.progress.tests
        touched = [*tests['changed_tests'], *tests['removed_tests']]
        kept = kept_tests(existing, touched)
        if not kept:
            return None
        return restore_finding(kept, reason), kept

    async def _restore_kept(self) -> None:
        """While verification of an API change passed but rejected its suite for tests of
        the repository it changed or removed that the changed service still passes, have
        the test author put them back as the repository holds them and verify again, at
        most MAX_RESTORE_ROUNDS times. A rejection left stays the verification's error."""
        for attempt in range(1, MAX_RESTORE_ROUNDS + 1):
            restore = restorable_tests(self.progress)
            if not restore or not self.progress.verification_error:
                return
            workflow.logger.info('Restore round %d: %d repository tests', attempt, len(restore))
            self.progress.verification_status = 'repairing'
            await self._generate_tests(
                review_request([restore_finding(restore)], 'tests', attempt, restore=restore))
            if self.progress.tests_error:
                # The rejected suite's image stays; the author's failure is the way on.
                self.progress.verification_status = 'completed'
                return
            self.progress.verification = None
            self.progress.verification_error = None
            self.progress.existing_tests = None
            await self._verify_and_repair()
            await self._prove_kept()

    async def _verify_correction(self, workers: list[str], number: int) -> bool:
        """Verify the delivery as the workers corrected it; true when it passed again."""
        if self.progress.implementation_error or self.progress.tests_error:
            self.progress.verification_status = 'error'
            return False
        if ('tests' in workers and self.spec.kind == 'bugfix'
                and not await self._reproduce(number)):
            # Corrected added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'error'
            return False
        self.progress.verification = None
        self.progress.verification_error = None
        self.progress.existing_tests = None
        await self._verify_and_repair()
        if self.spec.kind == 'api_change':
            await self._prove_kept()
        if self._passed():
            return True
        if self.progress.verification_status == 'completed':
            # The tests no longer pass: the delivery is not pushed this way.
            self.progress.verification_status = 'error'
            self.progress.verification_error = (
                self.progress.verification_error
                or 'Verification failed after correcting the review findings.')
        return False

    async def _instructed(self) -> None:
        """The user's instructions about the open review findings: a round of fixes from
        them, verification, and a new run of review rounds. If verification fails for
        good the earlier unverified push stays and the flag stays raised."""
        if self.spec.kind == 'docs':
            # A fix request on a documentation delivery: its author changes the
            # documentation as the comment asks, then the check; documentation is not
            # code-reviewed.
            await self._document(self.state.instructions)
            return
        # A fix request in the pull request comes with the open findings only while the
        # delivery has some.
        findings = (open_review_findings(self.progress)
                    if self.state.mention is None or self.progress.open_findings else [])
        number = len(self.progress.reviews) + 1
        instructions = self.state.instructions
        # Kept until the correction passes, so that retrying a failed author carries out
        # this fix again instead of the original task.
        self.state.code_fix = {
            'instructions': instructions, 'findings': findings, 'number': number,
            'mention': dict(self._mention_ref) if self._mention_ref else None}
        if await self._correct(findings, number, instructions):
            self.state.code_fix = None
            await self._review_rounds(instructions, findings)

    async def _retry_fix(self) -> None:
        """Retry the failed stage of a fix round from instructions or a mention: the
        failed author makes that fix again on the delivered sources, then verification
        and a new run of review rounds, as the round would have gone on."""
        fix = self.state.code_fix
        worker = 'implementation' if self.state.retry_stage == 'server' else 'tests'
        instructions = fix['instructions']
        number = fix['number']
        self._mention_ref = fix.get('mention')
        self.progress.verification_status = 'repairing'
        try:
            await (self._implement_service if worker == 'implementation'
                   else self._generate_tests)(
                review_request(fix['findings'], worker, number, instructions))
            if not await self._verify_correction([worker], number):
                return
            self.state.code_fix = None
            await self._review_rounds(instructions, fix['findings'])
        finally:
            self._mention_ref = None

    def _tests_failed(self) -> bool:
        return bool(self.progress.verification_status == 'completed'
                    and self.progress.verification
                    and self.progress.verification['status'] == 'failed')

    async def _verify_and_repair(self) -> None:
        """Verify; while tests fail, repair the side at fault and verify again."""
        await self._verify_service()
        if not self._tests_failed():
            return
        for attempt in range(1, MAX_REPAIR_ROUNDS + 1):
            if not await self._repair(attempt):
                return
            await self._verify_service(attempt)
            if not self._tests_failed():
                return
        self.progress.verification_error = (
            f'Tests still fail after {MAX_REPAIR_ROUNDS} automatic repair rounds.')

    async def _prove_kept(self) -> None:
        """An API change may rewrite the repository's tests, but only those its service
        no longer passes: run each one it changed or removed as the repository held it."""
        tests = self.progress.tests
        touched = [*tests['changed_tests'], *tests['removed_tests']]
        if (not touched or self.progress.verification_error
                or self.progress.verification_status != 'completed'
                or self.progress.verification['status'] != 'passed'):
            return
        self.progress.verification_status = 'running'
        try:
            image = await workflow.execute_activity(
                'build_repository_tests', args=[self.spec.session_id, self.spec.language],
                task_queue=VERIFICATION_TASK_QUEUE, result_type=dict,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self.progress.existing_tests = await workflow.execute_child_workflow(
                'VerificationWorkflow',
                args=[self.spec.session_id, self.progress.implementation['image_id'],
                      image['image_id'], self.spec.language, touched, SNAPSHOT_DIR],
                id=self._child_id('existing-tests'),
                task_queue=VERIFICATION_TASK_QUEUE,
                result_type=dict,
            )
        except (ActivityError, ChildWorkflowError) as exc:
            workflow.logger.warning('Run of the repository tests failed: %s', exc.cause or exc)
            self.progress.verification_error = (
                "The repository's tests could not be run against the changed service.")
        else:
            self.progress.verification_error = judge_kept(self.progress.existing_tests, touched)
            # Once seen passing as the repository holds them, they stay so for every
            # later author: a repair that rewrote one again would only be rejected again.
            self.state.kept_tests = sorted({
                *self.state.kept_tests, *kept_tests(self.progress.existing_tests, touched)})
        self.progress.verification_status = 'completed'

    async def _repair(self, attempt: int) -> bool:
        """Decide which side the failed tests condemn and have its own stage correct it."""
        self.progress.verification_status = 'diagnosing'
        try:
            diagnosis = await workflow.execute_child_workflow(
                'DiagnosisWorkflow',
                args=[self.spec.session_id, {**self._coding_context(),
                                             'verification': self.progress.verification}],
                id=self._child_id('diagnosis', attempt),
                task_queue=DIAGNOSIS_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Verification diagnosis failed: %s', exc.cause or exc)
            self.progress.verification_error = 'The failed tests could not be diagnosed.'
            self.progress.verification_status = 'completed'
            return False
        target = diagnosis['target']
        code_change = False
        if self.spec.onboarding and target == 'implementation':
            # The existing code changes only when the user confirms a bug in it.
            diagnosis, code_change = await self._confirm_bug(diagnosis)
            target = diagnosis['target']
        workflow.logger.info('Repair round %d: correcting the %s', attempt, target)
        self.progress.repairs.append(
            {'attempt': attempt, 'target': target, 'tests': diagnosis['tests'],
             'summary': diagnosis['summary']})
        self.progress.verification_status = 'repairing'
        repair = repair_request(diagnosis, self.progress.verification, attempt)
        if code_change:
            repair['code_change'] = True
        # A failed repair keeps the previous image and records the stage's own error.
        if target == 'implementation':
            await self._implement_service(repair)
        else:
            await self._generate_tests(repair)
        if self.progress.implementation_error or self.progress.tests_error:
            self.progress.verification_status = 'completed'
            return False
        if (target == 'tests' and self.spec.kind == 'bugfix'
                and not await self._reproduce(attempt)):
            # Repaired added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'completed'
            return False
        return True

    async def _confirm_bug(self, diagnosis: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Ask the user whether the failure the diagnosis traced to the existing code is a
        bug in it, and wait for the answer without a timeout. Returns the diagnosis as the
        answer settles it, and whether the existing code may change."""
        workflow_id = workflow.info().workflow_id
        summary = ' '.join(str(diagnosis.get('summary') or '').split())
        if len(summary) > BUG_SUMMARY_CHARS:
            summary = summary[:BUG_SUMMARY_CHARS - 1] + '…'
        question = BUG_QUESTION + (f' The diagnosis: {summary}' if summary else '')
        self._bug_answer = None
        self._bug_question = True
        self.progress.agent_questions = [
            *(item for item in self.progress.agent_questions
              if item['workflow_id'] != workflow_id),
            {'workflow_id': workflow_id, 'stage': 'diagnosis',
             'questions': [{'question': question,
                            'options': [BUG_CONFIRMED, SETUP_AT_FAULT, TESTS_AT_FAULT]}]}]
        await workflow.wait_condition(lambda: self._bug_answer is not None)
        self._bug_question = False
        self.progress.agent_questions = [item for item in self.progress.agent_questions
                                         if item['workflow_id'] != workflow_id]
        answer = self._bug_answer or ''
        workflow.logger.info('The user answered whether the failure is a bug: %s', answer[:80])
        own = '' if answer in (BUG_CONFIRMED, SETUP_AT_FAULT, TESTS_AT_FAULT) else (
            f' The user answered: {answer}')
        if answer == BUG_CONFIRMED or (own and answer.lower().startswith('yes')):
            return {**diagnosis, 'instructions': (
                f'{diagnosis["instructions"]}\n\nThe user confirmed that this is a bug in '
                f'the existing code: fix it there.{own}')}, True
        if answer == SETUP_AT_FAULT:
            return {**diagnosis, 'instructions': (
                f'{diagnosis["instructions"]}\n\nThe user said the existing code is right: '
                f'correct the Dockerfile or the service setup only.{own}')}, False
        failing = [{'class_name': item['class_name'], 'name': item['name']}
                   for item in (self.progress.verification or {}).get('test_failures') or []]
        return {**diagnosis, 'target': 'tests', 'tests': failing, 'instructions': (
            f'The user confirmed that the existing service behaves correctly here, so the '
            f'failing tests are wrong: correct them to expect what the service does, as far '
            f'as the contract allows. What the diagnosis found: '
            f'{diagnosis["summary"]}{own}')}, False

    async def _verify_service(self, repair: int = 0) -> None:
        self.progress.verification_status = 'running'
        change = self.spec.kind in CHANGE_KINDS
        # A run that could not complete says nothing about the code, so it runs once more.
        for attempt in range(1, VERIFICATION_ATTEMPTS + 1):
            try:
                self.progress.verification = await workflow.execute_child_workflow(
                    'VerificationWorkflow',
                    args=[self.spec.session_id, self.progress.implementation['image_id'],
                          self.progress.tests['image_id'], self.spec.language,
                          # The run of a change answers for every test of its suite.
                          self.progress.tests['added_tests'][:MAX_TRACKED] if change else None],
                    id=self._child_id('verification', repair),
                    task_queue=VERIFICATION_TASK_QUEUE,
                    result_type=dict,
                )
            except ChildWorkflowError as exc:
                workflow.logger.warning('Service verification failed: %s', exc.cause or exc)
                self.progress.verification_error = 'Service verification could not complete.'
                self.progress.verification_status = 'error'
                return
            if self.progress.verification['status'] != 'error':
                break
            workflow.logger.warning('Verification run %d could not complete: %s', attempt,
                                    self.progress.verification['error'])
        self.progress.verification_status = 'completed'
        if change and self.progress.verification['status'] == 'passed':
            # Red without the fix is only half of it: every test must be seen green.
            self.progress.verification_error = unproven_green(self.progress.verification)

    def _coding_context(self) -> dict[str, Any]:
        context = {
            "requirements": self.spec.requirements,
            "requirements_document": self.spec.requirements_document,
            "api_design": self.spec.api,
            "language": self.spec.language,
        }
        if self.spec.brief:
            # A change to an existing service: the contract above is the repository's.
            context['brief'] = self.spec.brief
        if self.spec.onboarding:
            # The contract was drafted from the repository's existing code.
            context['onboarding'] = True
        if self.spec.architecture:
            # The task's top-level architecture, which the code follows; the agents never
            # change it.
            context['architecture'] = self.spec.architecture
        return context

    def _stage_context(self, repair: dict[str, Any] | None) -> dict[str, Any]:
        context = self._coding_context()
        if self.spec.checkout:
            # The authors then work inside the session's checkout of this repository.
            context['repository'] = self.spec.repository
        if repair:
            context['repair'] = repair
        return context

    async def _publish(self) -> None:
        """Push the sources that passed verification, with their contract, to a new branch
        of the repository. Documentation that no check exists for yet is pushed too, and
        marked as unverified."""
        if not self.spec.checkout:
            return
        if self.progress.component_tests:
            # Neither the code review nor the push of a change with component tests is
            # there yet: one that passed both suites says so where its push would be.
            self.progress.publication_status = 'skipped'
            self.progress.pull_request_status = 'skipped'
            if self._components_passed():
                self.progress.publication_error = COMPONENT_CHANGE_UNPUBLISHED
            return
        # Tests that pass with review findings left after the last round are pushed too,
        # marked as unverified, with the findings in the commit message.
        open_findings = self.progress.verification_status == 'findings_open'
        result = (self.progress.verification
                  if self.progress.verification_status == 'completed' or open_findings
                  else None)
        documentation = self.spec.kind == 'docs'
        # Only a documentation task may go on without a check, and only when
        # verification says it has none: a check that ran and did not pass stops it too.
        unverified = bool(documentation and result and result['status'] == 'not_implemented')
        skip = not (unverified or result and result['status'] == 'passed')
        if not skip and self.spec.kind in CHANGE_KINDS and (
                self.progress.verification_error
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            # Green alone is not enough for a change: all of its tests must have run, a
            # bugfix's must have been red without the fix, and an API change must have
            # kept the repository's tests its service still passes.
            skip = True
        if skip:
            if ((self.progress.open_findings or self.state.mention is not None)
                    and self.progress.publication):
                # A run started by the user's instructions that did not pass leaves the
                # earlier unverified push on the branch, and the flag raised. So does a
                # fix round a pull request comment asked for, with the earlier push.
                self.progress.publication_status = 'completed'
                if (self.progress.pull_request_status in ('waiting', 'running')
                        and workflow.patched('kept-push-pull-request')):
                    # A retried repository stage, such as after a failed pull request
                    # step, still opens or updates the pull request of the earlier push;
                    # a step that was not attempted is never reported as done.
                    await self._pull_request()
                elif self.progress.pull_request_status in ('waiting', 'running'):
                    # Histories that predate the patch keep their pull request as it was.
                    self.progress.pull_request_status = (
                        'completed' if self.progress.pull_request else 'skipped')
            else:
                self.progress.publication_status = 'skipped'
                self.progress.pull_request_status = 'skipped'
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        findings = open_review_findings(self.progress) if open_findings else []
        if documentation:
            # The documentation alone: the service, its tests and its contract stay.
            push, args = publish_documentation, [self.spec.language, not unverified]
        else:
            contract = {
                'language': self.spec.language,
                'requirements': self.spec.requirements,
                'requirements_document': self.spec.requirements_document,
                'api': self.spec.api,
                # Committed as contracts/architecture.md and contracts/architecture.json;
                # without one the repository's files are kept.
                'architecture': self.spec.architecture,
                'architecture_structure': self.spec.architecture_structure,
            }
            if self.spec.brief:
                # The repository's contract grows by what the change required of the service.
                contract = with_requirements(contract, self.spec.brief['requirements'],
                                             self.spec.brief['retired'])
            if self.spec.onboarding:
                push, args = publish_onboarding, [
                    contract, findings, self.progress.existing_code_changed]
            else:
                push, args = publish_repository, [contract, self.spec.kind, findings]
        try:
            self.progress.publication = await workflow.execute_activity(
                push,
                args=[self.spec.session_id, self.spec.repository, *args],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                f'The {"documentation" if documentation else "verified sources"} could not be '
                'pushed to the repository.' + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
            # Nothing new was pushed, so no pull request step runs for it.
            self.progress.pull_request_status = 'skipped'
        else:
            workflow.logger.info('Sources pushed to %s', self.progress.publication['branch'])
            self.progress.publication_status = 'completed'
            self._pushed = True
            if not documentation:
                # A repeated push of a delivery whose findings the user accepted keeps
                # the acceptance; any other push with open findings asks the user again.
                accepted = (open_findings and self.state.retry_stage == 'repository'
                            and self.progress.findings_acceptance is not None
                            and not self.progress.open_findings)
                # Raised for the user to accept the delivery or give instructions.
                self.progress.open_findings = open_findings and not accepted
                if open_findings and not accepted:
                    self.progress.findings_acceptance = None
            if workflow.patched('pull-request'):
                await self._pull_request()
            else:
                # A delivery whose history predates the pull request step opens none;
                # the step ends here instead of waiting for good.
                self.progress.pull_request_status = 'not_applicable'

    async def _revise_architecture(self, instructions: str) -> bool:
        """Change the architecture of an architecture-only onboarding as a comment on its
        pull request asks; true when it changed and is to be pushed again."""
        try:
            revised = await workflow.execute_activity(
                'revise_onboarding_architecture',
                args=[self.spec.session_id, self.spec.architecture_structure, instructions],
                result_type=dict, start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Architecture fix failed: %s', exc.cause or exc)
            reason = (str(getattr(exc.cause, 'message', '') or '')[:2500]
                      if isinstance(exc.cause, ApplicationError) else '')
            self._fix_error = reason or 'The architecture could not be changed.'
            return False
        self.spec.architecture = revised['architecture']
        self.spec.architecture_structure = revised['architecture_structure']
        self._fix_summary = revised.get('summary') or None
        return True

    async def _publish_architecture(self) -> None:
        """Push the architecture an onboarding added to the repository's contract, then
        open its pull request."""
        if not self.spec.checkout:
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        try:
            self.progress.publication = await workflow.execute_activity(
                publish_onboarding_architecture,
                args=[self.spec.session_id, self.spec.repository, self.spec.architecture,
                      self.spec.architecture_structure],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Architecture push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                'The architecture could not be pushed to the repository.'
                + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
            self.progress.pull_request_status = 'skipped'
            return
        workflow.logger.info('Architecture pushed to %s', self.progress.publication['branch'])
        self.progress.publication_status = 'completed'
        self._pushed = True
        await self._pull_request()

    async def _pull_request(self) -> None:
        """Open the pull request of the pushed branch, or update the open one. Its failure
        never fails the push or the delivery: it is reported, and retrying the repository
        stage pushes again and tries again. A failed update keeps the pull request."""
        self.progress.pull_request_status = 'running'
        self.progress.pull_request_error = None
        brief = self.spec.brief
        change = {
            'kind': ('onboarding_architecture' if self.spec.architecture_only
                     else 'onboarding' if self.spec.onboarding else self.spec.kind),
            'requirements': brief['requirements'] if brief else self.spec.requirements,
            'retired': brief['retired'] if brief else [],
        }
        if self.spec.onboarding and self.progress.existing_code_changed:
            change['existing_code_changed'] = True
        args = [self.spec.session_id, self.spec.repository, self.progress.publication, change]
        text = None
        if workflow.patched('pull-request-text'):
            # The LLM writes the title and description; without them the generic text is
            # used, which is reported but fails neither the step nor the delivery.
            try:
                text = await workflow.execute_activity(
                    'write_pull_request_text',
                    args=[self.spec.session_id, self.progress.publication,
                          {**change, 'task': (brief['requirements_document'] if brief
                                              else self.spec.requirements_document)}],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                    result_type=dict,
                )
            except ActivityError as exc:
                workflow.logger.warning('The pull request text could not be written: %s',
                                        exc.cause or exc)
                # Only the activity's own explanations are shown: a provider's error
                # message may carry credentials (NFR-2).
                reason = (str(getattr(exc.cause, 'message', '') or '')[:500]
                          if isinstance(exc.cause, ApplicationError)
                          and exc.cause.type == 'PullRequestTextError' else '')
                self.progress.pull_request_text_error = (
                    'The LLM could not write the pull request title and description, so the '
                    'generic text was used.' + (f' {reason}' if reason else ''))
            args.append(text)
        try:
            self.progress.pull_request = await workflow.execute_activity(
                open_pull_request,
                args=args,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Pull request step failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.pull_request_error = (
                reason or 'The pull request could not be opened or updated.')
            self.progress.pull_request_status = 'error'
        else:
            workflow.logger.info('Pull request #%d is open', self.progress.pull_request['number'])
            self.progress.pull_request_status = 'completed'
            if text:
                # The pull request now carries LLM-written text.
                self.progress.pull_request_text_error = None

    @staticmethod
    def _stage_name(stage: str, repair: dict[str, Any] | None) -> str:
        """Corrections of review findings are told apart from repairs of failed tests."""
        return f'{stage}-review' if repair and 'review' in repair else stage

    async def _implement_service(self, repair: dict[str, Any] | None = None) -> None:
        try:
            self.progress.implementation = await workflow.execute_child_workflow(
                "ImplementationWorkflow",
                args=[self.spec.session_id, self._stage_context(repair)],
                id=self._child_id(self._stage_name('implementation', repair),
                                  repair['attempt'] if repair else 0),
                task_queue=IMPLEMENTATION_TASK_QUEUE,
                result_type=dict,
            )
            if self.spec.onboarding:
                self.progress.existing_code_changed = bool(
                    self.progress.implementation.get('code_changed'))
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service implementation failed: %s", exc.cause or exc)
            self.progress.implementation_error = "The service could not be implemented or built."

    async def _generate_tests(self, repair: dict[str, Any] | None = None) -> None:
        """Write the tests: the contract tests, and, for the first run of a change's tests
        with criteria routed to a seam, the component tests beside them. The contract
        tests' author is then given the criteria routed to it, and not run when there are
        none."""
        component = {} if repair else self._component_routes()
        routes = {requirement: {'route': route['route'], 'reason': route['reason']}
                  for requirement, route in (self.progress.routing or {}).items()
                  if route['route'] == 'contract'} if component else None
        authors = [self._generate_component_tests(component)] if component else []
        if routes or not component:
            authors.append(self._generate_contract_tests(repair, routes))
        await asyncio.gather(*authors)

    async def _generate_component_tests(self, routes: dict[str, Any]) -> None:
        self.progress.component_tests = None
        self.progress.component_tests_error = None
        try:
            tests = await workflow.execute_child_workflow(
                'ComponentTestGenerationWorkflow',
                args=[self.spec.session_id, {**self._stage_context(None), 'routes': routes}],
                id=self._child_id('component-test-generation'),
                task_queue=COMPONENT_TEST_GENERATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning('Component test generation failed: %s', exc.cause or exc)
            self.progress.component_tests_error = ('The component tests could not be '
                                                   'generated or built with the code.')
            return
        if 'unobservable' in tests:
            self._declined(tests['unobservable'])
            return
        self.progress.component_tests = tests

    async def _generate_contract_tests(self, repair: dict[str, Any] | None,
                                       routes: dict[str, Any] | None) -> None:
        # Tests put back as the repository holds them were seen passing so: they stay so.
        restore = (repair or {}).get('review', {}).get('restore') or []
        if restore:
            self.state.kept_tests = sorted({*self.state.kept_tests, *restore})
        context = self._stage_context(repair)
        if routes:
            # The criteria the contract tests prove, when the component tests prove others.
            context['routes'] = routes
        if self.state.kept_tests and self.spec.checkout:
            # The repository's tests the changed service was seen to pass as they are.
            context['keep'] = list(self.state.kept_tests)
        try:
            tests = await workflow.execute_child_workflow(
                "TestGenerationWorkflow",
                args=[self.spec.session_id, context],
                id=self._child_id(self._stage_name('test-generation', repair),
                                  repair['attempt'] if repair else 0),
                task_queue=TEST_GENERATION_TASK_QUEUE,
                result_type=dict,
            )
        except ChildWorkflowError as exc:
            workflow.logger.warning("Service test generation failed: %s", exc.cause or exc)
            self.progress.tests_error = "The service tests could not be generated or built."
            return
        if 'unobservable' in tests:
            # No suite was written: the delivery stops, as when the routing found these.
            self._declined(tests['unobservable'])
            return
        self.progress.tests = tests
