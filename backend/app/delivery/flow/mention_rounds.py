from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.delivery.models import (
        DELIVERY_CLOSING, MENTION_HANDLED, OUTCOME_OWED, STOPPED, is_mention, mention_context,
        mention_key,
    )
    from app.github.models import PullRequestMention

# Says on the earlier pull request where the fix a comment asked for went.
NEW_PULL_REQUEST = 'It was opened as a new pull request: {url}'
# How the LLM may classify a mention of the app.
MENTION_CLASSES = ('fix', 'clarification', 'unclear')
# Statuses of a mention that has ended; it then lives in the mention history alone.
MENTION_ENDED = ('replied', 'completed', 'error')
UNCLEAR_REPLY = ('I could not tell what you would like me to do. Mention me again and say '
                 'whether you are asking for a fix to the code of this pull request or for '
                 'an explanation of it.')
FIX_STARTED = ('Started a fix round from your comment: the changes, then verification and '
               'code review. I will reply here again with the outcome.')
DOCS_FIX_STARTED = ('Started a fix round of the documentation from your comment: the '
                    'changes, then their check. I will reply here again with the outcome.')
ARCHITECTURE_FIX_STARTED = ('Started a fix round of the architecture from your comment: '
                            'contracts/architecture.md and contracts/architecture.json are '
                            'changed as it asks and pushed to this pull request, without '
                            'tests or code review. I will reply here again with the outcome.')


class MentionRounds(FlowBase):
    """Mentions of the app on the delivery's pull request: classified, answered
    or queued as fix rounds, and replied to when their round ends."""

    @workflow.update
    async def mention(self, mention: PullRequestMention) -> None:
        """An authorised mention of the app on the delivery's pull request: classified,
        then answered right away from the code as last pushed, or queued as a fix round
        that runs after the current one, in the order the requests arrived. Its full text
        is kept in the mention history, never in the workflow's state."""
        body = mention.get('body')
        assert body is not None  # handed over with its text; only the workflow drops it
        # A mention on a closed delivery's pull request was classified as a fix before
        # the flow that carries it on was started for it.
        preset = mention.get('classification')
        self._handed_over = True
        entry = cast(PullRequestMention, {
            **{key: value for key, value in mention.items() if key != 'body'},
            'classification': 'pending', 'status': 'classifying'})
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
        await self._handle_mention(entry, body, preset)
        if entry['status'] in MENTION_ENDED:
            await self._settle(entry)

    async def _handle_mention(self, entry: PullRequestMention, body: str,
                              preset: object = None) -> None:
        if preset == 'fix' and workflow.patched('classified-fix-mentions'):
            classification = 'fix'
        else:
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
        if self._closing() is not None:
            raise ValueError(DELIVERY_CLOSING)
        if any(item['comment_id'] == mention['comment_id']
               and item['comment_type'] == mention['comment_type']
               for item in self.progress.mentions):
            raise ValueError(MENTION_HANDLED)

    def _fix_unavailable(self) -> str | None:
        """Why the delivery cannot take a fix round, if it cannot."""
        if self.spec.architecture_only:
            workflow.deprecate_patch('architecture-fixes')
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
        comment_type, comment_id = key
        if comment_id is None:
            return None
        return next((item for item in self.progress.mentions
                     if is_mention(item, (comment_type, comment_id)) and item['classification'] == 'fix'
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
            progress.component_tests_error = None
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
        return mention_context(self.spec, self.progress)

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
            assert publication is not None
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
            pull = self.progress.pull_request
            if (pull and entry.get('pull_number') and pull.get('url')
                    and pull.get('number') != entry.get('pull_number')):
                # The comment was made on an earlier pull request, such as one that was
                # merged or closed: the reply there links to the new one.
                pushed_to = f'{pushed_to} {NEW_PULL_REQUEST.format(url=pull["url"])}'
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
