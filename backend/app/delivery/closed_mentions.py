"""Mentions of the app on the pull request of a delivery whose flow closed with it.

Such a delivery does not run again, and only a fix request starts a new flow: the fix
round's task flow, on a new branch. A mention for which no flow of a fix round runs yet is
first classified here, for the closed delivery: a question is answered and an unclear
comment told so right away in the pull request, from the code as last pushed, and kept
with the closed delivery's mentions, without starting any flow; a fix request is handed
on to the fix round's new flow by the handover that started this workflow. Safe to import
from workflow code."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from app.delivery.flow.mention_rounds import MENTION_CLASSES, UNCLEAR_REPLY

WORKFLOW_TYPE = 'ClosedMentionWorkflow'
# What the workflow reports besides a classification: the comment was recorded before
# by another handover, or it could not be recorded or classified (and was replied to).
HANDLED = 'handled'
FAILED = 'error'


def closed_mention_id(session_id: str, comment_type: str, comment_id: int) -> str:
    """One workflow per comment, so that a handover tried again finds the one it started.
    It has no ':' in it, which separates the run from the handover in the recording."""
    return f'closed-mention-{session_id}-{comment_type}-{comment_id}'


@dataclass
class ClosedMention:
    session_id: str
    # The mention as handed over, with its full text.
    mention: dict[str, Any]
    # What an answer is based on besides the code, as of the closed delivery.
    context: dict[str, Any] = field(default_factory=dict)
    # Why the closed delivery cannot be fixed, such as when it was never pushed.
    fix_problem: str | None = None


@workflow.defn
class ClosedMentionWorkflow:
    def __init__(self) -> None:
        self._classification: str | None = None

    @workflow.query
    def classification(self) -> str | None:
        """None until the mention was classified; then its class, HANDLED or FAILED."""
        return self._classification

    @workflow.run
    async def run(self, start: ClosedMention) -> str:
        mention = start.mention
        body = str(mention.get('body') or '')
        entry: dict[str, Any] = {
            **{key: value for key, value in mention.items() if key != 'body'},
            'classification': 'pending', 'status': 'classifying'}
        recording = f'{workflow.info().run_id}:{workflow.info().workflow_id}'
        try:
            added = await workflow.execute_activity(
                'record_new_mention', args=[start.session_id, {**entry, 'body': body},
                                            recording],
                result_type=bool, start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5))
        except ActivityError as exc:
            workflow.logger.warning('Mention not recorded: %s', exc.cause or exc)
            await self._failed(start, entry, 'The comment could not be recorded.',
                               save=False)
            return self._done(FAILED)
        if not added:
            return self._done(HANDLED)
        try:
            classification = await workflow.execute_activity(
                'classify_mention', args=[start.session_id, body],
                result_type=str, start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            workflow.logger.warning('Mention not classified: %s', exc.cause or exc)
            await self._failed(start, entry, 'The comment could not be classified.')
            return self._done(FAILED)
        if classification not in MENTION_CLASSES:
            classification = 'unclear'
        entry['classification'] = classification
        workflow.logger.info('Mention %s on the closed pull request of %s classified as %s',
                             entry.get('comment_id'), start.session_id, classification)
        if classification == 'fix':
            if start.fix_problem:
                await self._failed(start, entry, start.fix_problem)
                return self._done(FAILED)
            # The fix round's flow takes it; until then it waits as queued.
            entry['status'] = 'queued'
            await self._save(start, entry)
            return self._done('fix')
        # Answered right away; nothing is started for it.
        self._classification = classification
        if classification == 'clarification':
            try:
                text = await workflow.execute_activity(
                    'answer_mention', args=[start.session_id, body, start.context],
                    result_type=str, start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=2))
            except ActivityError as exc:
                workflow.logger.warning('Mention not answered: %s', exc.cause or exc)
                await self._failed(start, entry, 'The question could not be answered.')
                return classification
        else:
            text = UNCLEAR_REPLY
        if await self._reply(start, entry, text):
            entry['status'] = 'replied'
        await self._save(start, entry)
        return classification

    def _done(self, result: str) -> str:
        self._classification = result
        return result

    async def _reply(self, start: ClosedMention, entry: dict[str, Any], text: str) -> bool:
        """Reply in the pull request the comment was made on; false when that failed."""
        repository = start.context.get('repository') or {}
        full_name = entry.get('repository') or (
            repository.get('full_name') if isinstance(repository, dict) else None)
        number = entry.get('pull_number')
        if not full_name or not number:
            entry['status'] = 'error'
            entry['error'] = 'The delivery has no pull request to reply in.'
            return False
        reply_to = entry.get('reply_to') or entry['comment_id']
        try:
            entry['reply_url'] = await workflow.execute_activity(
                'reply_to_mention',
                args=[full_name, number, entry['comment_type'], reply_to, entry['author'],
                      text],
                result_type=str, start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            workflow.logger.warning('Reply to mention failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            entry['status'] = 'error'
            entry['error'] = reason or 'The reply could not be posted in the pull request.'
            return False
        return True

    async def _failed(self, start: ClosedMention, entry: dict[str, Any], error: str,
                      save: bool = True) -> None:
        await self._reply(start, entry, f'Sorry, this could not be done: {error}')
        entry['status'] = 'error'
        entry['error'] = error
        if save:
            await self._save(start, entry)

    async def _save(self, start: ClosedMention, entry: dict[str, Any]) -> None:
        try:
            await workflow.execute_activity(
                'save_mention', args=[start.session_id, entry],
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5))
        except ActivityError as exc:
            workflow.logger.warning('Mention %s not saved: %s', entry.get('comment_id'),
                                    exc.cause or exc)
