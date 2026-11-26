"""The automatic fix round of a pull request that conflicts with its base branch."""
import re
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from app.delivery.flow.base import FlowBase
from app.delivery.flow.merge_conflicts import MAX_MERGE_FIX_ROUNDS

# Marks the workflow code that checks, while the delivery waits, whether its pull request
# conflicts with its base branch, and merges the base branch in a fix round of its own.
BASE_MERGE_PATCH = 'base-merge-rounds'
# How many conflicting files a pull request comment names.
LISTED_FILES = 20
BASE_MERGE_INSTRUCTIONS = """The pull request of this delivery conflicts with its base
branch {base}, whose tip is now {commit}: the base branch changed these files since the
delivery's branch was made, and so did this delivery:

{files}

Change the sources so that they keep what this delivery does and also take in what the
base branch changed, as a merge would. The base branch is then merged into the pull
request branch, and these files are taken as you leave them, in place of the base
branch's versions, so a change of the base branch you leave out is lost. Where both
changed the same lines, combine them; where they contradict, keep the behaviour this
delivery's contract asks for. The paths are the repository's: the service's files are
where the repository has them, and contract-tests/ is the contract test suite. Leave
the files under contracts/ as they are: the merge combines the base branch's
requirements, API and architecture there with this delivery's on its own. What the base
branch changed in these files, from where the branches parted:

{changes}"""
BASE_MERGE_STARTED = ('This pull request conflicts with its base branch `{base}` in {files}. '
                      'Started automatic fix round {round} of {limit} to merge `{base}` into '
                      'it: {steps}, then the merge is pushed to this branch.')
BASE_MERGE_STEPS_FULL = ('the changes that take in what `{base}` changed, then verification '
                         'and code review')
BASE_MERGE_STEPS_DOCS = 'the documentation changes that take in what `{base}` changed'
BASE_MERGE_STEPS_ARCHITECTURE = 'the architecture is kept as this pull request has it'
BASE_MERGE_REMAINS = ('This pull request still conflicts with its base branch `{base}` in '
                      '{files}. {reason} Merge `{base}` into this branch, or mention me with '
                      'what to fix.')
BASE_MERGE_LIMIT = ('After {rounds} automatic fix rounds in a row no further one starts on '
                    'its own.')


def _named(files: list[str]) -> str:
    named = ', '.join(f'`{item}`' for item in files[:LISTED_FILES])
    if len(files) > LISTED_FILES:
        named += f' and {len(files) - LISTED_FILES} more files'
    return named or 'its files'


class BaseMerge(FlowBase):
    """A waiting delivery's or onboarding's pull request that conflicts with its base
    branch: a fix round of its own takes the base branch's changes into the conflicting
    files and pushes a merge of the base branch's tip into the pull request branch. Its
    rounds count with those of push conflicts towards MAX_MERGE_FIX_ROUNDS in a row."""

    async def _check_conflicts(self) -> bool:
        """Ask GitHub whether the waiting delivery's open pull request conflicts with its
        base branch, and run a base merge fix round when it does; a failed check changes
        nothing. True when a round ran."""
        pull, repository = self.progress.pull_request, self.spec.repository
        if (not self._checks_pull_request() or pull is None or repository is None
                or not self._ready_to_merge()):
            return False
        if not workflow.patched(BASE_MERGE_PATCH):
            return False
        try:
            found = await workflow.execute_activity(
                'pull_request_conflicts', args=[repository['full_name'], pull['number']],
                result_type=dict, start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Pull request conflicts not read: %s', exc.cause or exc)
            return False
        if not isinstance(found, dict):
            return False
        if found.get('conflicting') is False:
            if found.get('pending') is not True:
                # The pull request merges cleanly: rounds in a row end here, and a later
                # conflict starts its count over. While GitHub has not computed it yet,
                # such as right after a push, the row goes on.
                self._end_merge_row()
            return False
        if found.get('conflicting') is not True:
            return False
        # A fix round requested meanwhile, or the end of the pull request, goes first.
        if not self._ready_to_merge():
            return False
        return await self._base_merge_round(found)

    def _end_merge_row(self) -> None:
        """Count the next automatic merge fix round from 1 again, and say anew when it
        cannot start, unless a merge fix round is running or queued."""
        if self.state.merge_fix is not None or self._push_conflict is not None:
            return
        self._merge_round = None
        self._base_merge_stopped = False

    def _ready_to_merge(self) -> bool:
        return (self.state.waiting and self._next is None and self._closing() is None
                and not self._fix_pending())

    def _base_merge_refused(self) -> str | None:
        if not self.spec.checkout or not self.progress.publication:
            return 'Nothing of this delivery was pushed to merge the base branch into.'
        if (not self.spec.architecture_only and self.spec.kind != 'docs'
                and not self.spec.language):
            return 'No implementation language was chosen for this delivery.'
        return None

    async def _base_merge_round(self, found: dict[str, Any]) -> bool:
        """Run the base merge fix round of the conflicts `found`, unless the rounds in a
        row reached their limit; true when it ran."""
        base = str(found.get('base') or 'the base branch')
        commit = str(found.get('base_commit') or '')
        files = [str(item) for item in found.get('files') or []]
        if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            workflow.logger.warning('Base branch tip unknown; no merge round')
            return False
        round_ = (self._merge_round['round'] if self._merge_round else 0) + 1
        refused = (BASE_MERGE_LIMIT.format(rounds=round_ - 1)
                   if round_ > MAX_MERGE_FIX_ROUNDS else self._base_merge_refused())
        if refused:
            if not self._base_merge_stopped:
                # Said once; the delivery waits for the user.
                self._base_merge_stopped = True
                await self._ci_comment(BASE_MERGE_REMAINS.format(
                    base=base, files=_named(files), reason=refused))
            return False
        steps = (BASE_MERGE_STEPS_ARCHITECTURE if self.spec.architecture_only
                 else BASE_MERGE_STEPS_DOCS if self.spec.kind == 'docs'
                 else BASE_MERGE_STEPS_FULL).format(base=base)
        await self._ci_comment(BASE_MERGE_STARTED.format(
            base=base, files=_named(files), round=round_, limit=MAX_MERGE_FIX_ROUNDS,
            steps=steps))
        fix = {'commit': commit, 'files': files, 'round': round_}
        self._merge_round = fix
        self.state.merge_fix = dict(fix)
        self.state.waiting = False
        self.state.retry_stage = None
        self.state.record_only = False
        self._pushed = False
        progress = self.progress
        published = progress.publication
        progress.agent_questions = []
        progress.publication_error = None
        merged: dict[str, Any] | None = None
        error: str | None = None
        try:
            if self.spec.architecture_only:
                # No agent: the architecture stays as the pull request has it.
                merged, error = await self._push_base_merge(fix)
            else:
                progress.review_error = None
                progress.correction_failed = False
                progress.implementation_error = None
                progress.tests_error = None
                progress.component_tests_error = None
                progress.documentation_error = None
                progress.verification_status = 'repairing'
                self.state.instructions = BASE_MERGE_INSTRUCTIONS.format(
                    base=base, commit=commit,
                    files='\n'.join(f'- {item}' for item in files) or '(not listed)',
                    changes=str(found.get('changes') or '') or '(not available)')
                await self._deliver()
                if self._pushed and progress.publication_status == 'completed':
                    merged, error = await self._push_base_merge(fix)
                else:
                    error = (progress.documentation_error or progress.implementation_error
                             or progress.tests_error or progress.component_tests_error
                             or progress.baseline_error
                             or progress.verification_error or progress.review_error
                             or progress.publication_error
                             or self._unpushed_reason())
            if merged is not None:
                previous = progress.publication or published or {}
                merged['verified'] = bool(previous.get('verified', True))
                progress.publication = merged
                progress.publication_status = 'completed'
                progress.publication_error = None
                self._pushed = True
                branch = merged.get('branch') or previous.get('branch') or 'this branch'
                await self._ci_comment(
                    f'Automatic fix round {round_} merged `{base}` into `{branch}` and '
                    f'pushed the merge commit `{str(merged["commit"])[:12]}`.')
            else:
                await self._ci_comment(
                    f'Automatic fix round {round_} for the conflicts with `{base}` ended '
                    f'without a push: {error} The branch keeps its earlier push.')
        finally:
            self.state.merge_fix = None
            self.state.instructions = None
        # Reports that arrived during the round are answered by its push.
        self._conflict_requested = False
        self.state.waiting = True
        # A fix request that arrived during the round starts its round now, and then one
        # of remote commits the round's own push conflicted with.
        self._start_queued()
        await self._start_merge_fix()
        return True

    async def _push_base_merge(
            self, fix: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Push the merge of the base branch's tip into the pull request branch: the
        merge, or why it failed."""
        progress = self.progress
        progress.publication_status = 'running'
        try:
            merged = await workflow.execute_activity(
                'publish_base_merge',
                args=[self.spec.session_id, self.spec.repository, fix['commit'],
                      fix['files']],
                result_type=dict, start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Base branch merge not pushed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            error = ('The base branch could not be merged into the pull request branch.'
                     + (f' {reason}' if reason else ''))
            # The branch keeps its earlier push.
            progress.publication_status = 'completed'
            progress.publication_error = error
            return None, error
        if not isinstance(merged, dict) or not merged.get('commit'):
            progress.publication_status = 'completed'
            error = 'The base branch merge returned no commit.'
            progress.publication_error = error
            return None, error
        workflow.logger.info('Base branch merged into %s', merged.get('branch'))
        return merged, None
