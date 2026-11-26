"""The automatic fix rounds of merge conflicts: of a push that commits on its remote
branch conflicted with, and of a pull request that conflicts with its base branch."""
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.config import DOCUMENTATION_TASK_QUEUE
    from app.delivery.causes import with_cause
    from app.delivery.models import DeliveryState
    from app.github.models import PUSH_CONFLICT

# Marks the workflow code that starts a fix round of its own when commits on the
# delivery's remote branch conflict with what it pushes, and pushes the round's result
# as the merge of those commits.
MERGE_FIX_PATCH = 'merge-conflict-rounds'
# Automatic merge fix rounds in a row; a push the user asks for starts the count over.
MAX_MERGE_FIX_ROUNDS = 2
MERGE_FIX_INSTRUCTIONS = """The branch {branch} on GitHub has commits this delivery did
not push (its tip is {commit}), such as changes made in the pull request or a merge of
its base branch, and they conflict with the sources this delivery pushes in these files:

{files}

Change the sources so that they keep what this delivery does and also take in what those
commits changed, as a merge would. The push takes these files as you leave them, in
place of the commits' versions, so a change of theirs you leave out is lost. Where both
changed the same lines, combine them; where they contradict, keep the behaviour this
delivery's contract asks for. The paths are the repository's: the service's files are
where the repository has them, and contract-tests/ is the contract test suite. Files
under contracts/ are written from the delivery's contract and keep its version. What
those commits changed in these files, from where the histories parted:

{changes}"""
MERGE_FIX_STARTED = ('Commits on `{branch}` that this delivery did not push conflict with '
                     'its push in {files}. Started automatic fix round {round} of {limit} '
                     'to take them in: the changes, then verification and code review.')
MERGE_FIX_REFUSED_HINT = ('Delete that branch on GitHub, or revert the conflicting commits '
                          'on it, then retry the push.')

# Marks the workflow code that checks whether the waiting delivery's pull request
# conflicts with its base branch and, when it does, merges the base branch into the pull
# request branch in a fix round of its own.
BASE_MERGE_PATCH = 'base-merge-rounds'
# How the instructions of a base merge fix round begin: they tell such a round apart
# from a round of conflicting commits on the pull request branch.
BASE_MERGE_PREFIX = 'The base branch '
BASE_MERGE_INSTRUCTIONS = BASE_MERGE_PREFIX + """{base} of this delivery's pull request
has moved on (its tip is {commit}), and the pull request now conflicts with it in these
files:

{files}

The base branch is merged into the pull request branch. Change the sources so that they
keep what this delivery does and also take in what the base branch changed in these
files, as a merge would. The merge takes these files as you leave them, in place of the
base branch's versions, so a change of the base branch you leave out is lost. Where both
changed the same lines, combine them; where they contradict, keep the behaviour this
delivery's contract asks for. The paths are the repository's: the service's files are
where the repository has them, and contract-tests/ is the contract test suite. Files
under contracts/ are written from the delivery's contract and keep its version. What the
base branch changed in these files, from where the histories parted:

{changes}"""
BASE_MERGE_STARTED = ('This pull request conflicts with its base branch `{base}` in {files}. '
                      'Started automatic fix round {round} of {limit} to merge `{base}` into '
                      'it: {steps}.')
BASE_MERGE_STEPS_FULL = ('the changes, then verification and code review, then the push of '
                         'the merge')
BASE_MERGE_STEPS_DOCS = 'the changes, then the push of the merge'
BASE_MERGE_STEPS_ARCHITECTURE = 'the push of the merge, keeping the architecture as it is'
BASE_MERGE_REMAINS = ('This pull request still conflicts with its base branch `{base}` in '
                      '{files}. {reason} Merge `{base}` into the branch by hand, or mention '
                      'the app with what to do.')
BASE_MERGE_FAILED = 'The merge of the base branch could not be pushed.'


def is_base_merge(state: DeliveryState) -> bool:
    """Whether the run of `state` is a base merge fix round."""
    return (state.merge_fix is not None
            and (state.instructions or '').startswith(BASE_MERGE_PREFIX))


def push_conflict(exc: ActivityError) -> dict[str, Any] | None:
    """The details of a push that remote commits conflicted with, from its failure."""
    cause = exc.cause
    if not isinstance(cause, ApplicationError) or cause.type != PUSH_CONFLICT:
        return None
    details = cause.details[0] if cause.details else None
    if not isinstance(details, dict) or not isinstance(details.get('commit'), str):
        return None
    return {'commit': details['commit'], 'branch': str(details.get('branch') or ''),
            'files': [str(item) for item in details.get('files') or []],
            'changes': str(details.get('changes') or '')}


class MergeConflicts(FlowBase):
    """Commits on the delivery's remote branch that conflict with its push: a fix round of
    the delivery's own takes them into the sources, which are verified and reviewed
    again, and its push merges those commits with the conflicting files as the round left
    them. Up to MAX_MERGE_FIX_ROUNDS rounds run in a row, as the branch may move again."""

    def _merge_conflicted(self, conflict: dict[str, Any] | None) -> str:
        """What the failed push's error adds: that a fix round starts once the run ends,
        or else what the user can do."""
        if conflict is None or not workflow.patched(MERGE_FIX_PATCH):
            return f' {MERGE_FIX_REFUSED_HINT}' if conflict is not None else ''
        refused = self._merge_fix_refused()
        if refused:
            return f' {refused} {MERGE_FIX_REFUSED_HINT}'
        self._push_conflict = conflict
        return ' An automatic fix round takes them into the sources and pushes again.'

    def _merge_fix_refused(self) -> str | None:
        if self.spec.architecture_only:
            return 'The architecture is not fixed by a round of its own.'
        if self.spec.kind != 'docs' and not self.spec.language:
            return 'No implementation language was chosen for this delivery.'
        fix = self._merge_round
        if fix is not None and fix['round'] >= MAX_MERGE_FIX_ROUNDS:
            return (f'After {fix["round"]} automatic fix rounds in a row no further one '
                    'starts on its own.')
        return None

    async def _start_merge_fix(self) -> None:
        """Start the fix round of this run's conflicted push, once the delivery waits and
        unless a fix requested in a comment starts first: its push meets the same
        commits and starts the round then."""
        conflict, self._push_conflict = self._push_conflict, None
        if conflict is None or self._next is not None or not self.state.waiting:
            return
        round_ = (self._merge_round['round'] if self._merge_round else 0) + 1
        branch = conflict['branch']
        files = ', '.join(f'`{item}`' for item in conflict['files'][:20])
        await self._ci_comment(MERGE_FIX_STARTED.format(
            branch=branch, files=files, round=round_, limit=MAX_MERGE_FIX_ROUNDS))
        progress = self.progress
        progress.agent_questions = []
        progress.review_error = None
        progress.correction_failed = False
        progress.implementation_error = None
        progress.tests_error = None
        progress.component_tests_error = None
        progress.documentation_error = None
        progress.verification_status = 'repairing'
        self.state.merge_fix = {'commit': conflict['commit'], 'files': conflict['files'],
                                'round': round_}
        self.state.instructions = MERGE_FIX_INSTRUCTIONS.format(
            branch=branch, commit=conflict['commit'],
            files='\n'.join(f'- {item}' for item in conflict['files']),
            changes=conflict['changes'] or '(not available)')
        self.state.waiting = False
        self.state.retry_stage = None
        self.state.record_only = False
        self._next = self.state

    async def _merge_fix_finished(self) -> None:
        """Say in the pull request how this run's merge fix round ended."""
        fix = self._merge_round
        assert fix is not None  # only a merge fix round finishes one
        self.state.merge_fix = None
        self.state.instructions = None
        if self._base_round:
            await self._base_merge_finished(fix)
            return
        publication = self.progress.publication
        if self._pushed and self.progress.publication_status == 'completed' and publication:
            await self._ci_comment(
                f'Automatic fix round {fix["round"]} took the conflicting commits in and '
                f'pushed the merge to `{publication["branch"]}` at commit '
                f'`{publication["commit"][:12]}`.')
            return
        error = (self.progress.documentation_error or self.progress.implementation_error
                 or self.progress.tests_error or self.progress.component_tests_error
                 or self.progress.baseline_error
                 or self.progress.verification_error or self.progress.review_error
                 or self.progress.publication_error
                 or self._unpushed_reason())
        await self._ci_comment(
            f'Automatic fix round {fix["round"]} for the conflicting commits ended without '
            f'a push: {error}')

    # The pull request's conflicts with its base branch: a fix round merges the base branch
    # into the pull request branch.

    def _base_name(self) -> str:
        pull = self.progress.pull_request or {}
        return str(pull.get('base') or 'the base branch')

    def _base_merge_refused(self) -> str | None:
        """Why no base merge fix round starts on its own, if none does."""
        if not self.spec.repository:
            return 'The delivery has no repository to push the merge to.'
        if (not self.spec.architecture_only and self.spec.kind != 'docs'
                and not self.spec.language):
            return 'No implementation language was chosen for this delivery.'
        fix = self._merge_round
        if fix is not None and fix['round'] >= MAX_MERGE_FIX_ROUNDS:
            return (f'After {fix["round"]} automatic fix rounds in a row no further one '
                    'starts on its own.')
        return None

    async def _check_base_conflicts(self) -> None:
        """Ask GitHub whether the waiting delivery's pull request conflicts with its base
        branch and, when it does, start a fix round that merges the base branch in, unless
        one is already queued or no further one may start, which is said once in the
        pull request. A failed check changes nothing."""
        if not workflow.patched(BASE_MERGE_PATCH):
            return
        pull, repository = self.progress.pull_request, self.spec.repository
        if (not pull or not pull.get('number') or not repository
                or not repository.get('full_name') or not self.state.waiting
                or self._next is not None or self._closing() is not None):
            return
        try:
            found = await workflow.execute_activity(
                'pull_request_conflicts', args=[repository['full_name'], pull['number']],
                result_type=dict, start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Base branch conflicts not read: %s', exc.cause or exc)
            return
        if not isinstance(found, dict) or found.get('conflicting') is not True:
            self._base_conflict_told = False
            return
        if (self._next is not None or not self.state.waiting
                or self._closing() is not None):
            # A round is already queued, or the delivery stopped waiting meanwhile.
            return
        files = [str(item) for item in found.get('files') or []]
        named = ', '.join(f'`{item}`' for item in files[:20]) or 'some files'
        base = str(found.get('base') or self._base_name())
        refused = self._base_merge_refused()
        if refused:
            if not self._base_conflict_told:
                self._base_conflict_told = True
                workflow.logger.info('Pull request conflicts with its base; %s', refused)
                await self._ci_comment(BASE_MERGE_REMAINS.format(
                    base=base, files=named, reason=refused))
            return
        commit = str(found.get('base_commit') or '')
        if not commit:
            return
        round_ = (self._merge_round['round'] if self._merge_round else 0) + 1
        steps = (BASE_MERGE_STEPS_ARCHITECTURE if self.spec.architecture_only
                 else BASE_MERGE_STEPS_DOCS if self.spec.kind == 'docs'
                 else BASE_MERGE_STEPS_FULL)
        workflow.logger.info('Pull request conflicts with %s; base merge round %d',
                             base, round_)
        await self._ci_comment(BASE_MERGE_STARTED.format(
            base=base, files=named, round=round_, limit=MAX_MERGE_FIX_ROUNDS, steps=steps))
        if self._next is not None or not self.state.waiting:
            # The user's decision or a fix request came first; it pushes onto the same
            # branch, and a later check finds whether the conflict remains.
            return
        progress = self.progress
        progress.agent_questions = []
        progress.review_error = None
        progress.correction_failed = False
        progress.implementation_error = None
        progress.tests_error = None
        progress.component_tests_error = None
        progress.documentation_error = None
        if not self.spec.architecture_only and self.spec.kind != 'docs':
            progress.verification_status = 'repairing'
        self._base_merge_record({'round': round_, 'limit': MAX_MERGE_FIX_ROUNDS,
                                 'base': base, 'base_commit': commit, 'files': files,
                                 'status': 'running', 'commit': None, 'error': None})
        self.state.merge_fix = {'commit': commit, 'files': files, 'round': round_}
        self.state.instructions = BASE_MERGE_INSTRUCTIONS.format(
            base=base, commit=commit, files='\n'.join(f'- {item}' for item in files),
            changes=str(found.get('changes') or '') or '(not available)')
        self.state.waiting = False
        self.state.retry_stage = None
        self.state.record_only = False
        self._next = self.state

    def _base_merge_record(self, merge: dict[str, Any]) -> None:
        """Show the base merge round and how it stands in the delivery's progress, with
        the publication it pushes onto."""
        if self.progress.publication is not None:
            self.progress.publication = {**self.progress.publication, 'base_merge': merge}

    def _base_merge_update(self, **changes: Any) -> None:
        publication = self.progress.publication
        merge = publication.get('base_merge') if publication else None
        if isinstance(merge, dict):
            self._base_merge_record({**merge, **changes})

    async def _base_merge(self) -> None:
        """This run's base merge fix round: the delivery's agent settles the conflicting
        files, followed for code by verification, code review and the push of the
        sources; then the base branch is merged into the pull request branch with the
        conflicting files as the round left them. Documentation skips the check, and an
        architecture-only onboarding runs no agent: its merge keeps its architecture."""
        fix = self.state.merge_fix
        assert fix is not None  # only a base merge round runs one
        if self.spec.architecture_only:
            self.progress.verification_status = 'not_applicable'
        elif self.spec.kind == 'docs':
            if not await self._base_merge_documentation():
                return
        else:
            merge = (self.progress.publication or {}).get('base_merge')
            await self._deliver()
            if isinstance(merge, dict):
                # The push of the sources replaced the publication the round shows on.
                self._base_merge_record(merge)
            if not (self._pushed and self.progress.publication_status == 'completed'):
                # Verification, review or the push of the sources did not pass: the
                # earlier push stays and the outcome says why.
                return
        await self._push_base_merge(fix)

    async def _base_merge_documentation(self) -> bool:
        """The documentation author settles the conflicting files; no check follows."""
        context = self._stage_context(None)
        context['fix_request'] = {'instructions': self.state.instructions}
        self.progress.documentation_error = None
        status = self.progress.verification_status
        self.progress.verification_status = 'running'
        try:
            self.progress.documentation = await self._child(
                'DocumentationWorkflow', DOCUMENTATION_TASK_QUEUE,
                self._child_id('documentation'), context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('Documentation merge failed: %s', exc.cause or exc)
            self.progress.documentation_error = with_cause(
                'The documentation could not be merged with the base branch.', exc)
            return False
        finally:
            self.progress.verification_status = status
        return True

    async def _push_base_merge(self, fix: dict[str, Any]) -> None:
        """Push the merge of the base branch's tip into the pull request branch."""
        self.progress.publication_status = 'running'
        try:
            result = await workflow.execute_activity(
                'publish_base_merge',
                args=[self.spec.session_id, self.spec.repository, fix['commit'],
                      fix['files']],
                result_type=dict, start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Base merge not pushed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self._base_merge_error = BASE_MERGE_FAILED + (f' {reason}' if reason else '')
            self.progress.publication_status = (
                'completed' if self.progress.publication else 'error')
            return
        merge = (self.progress.publication or {}).get('base_merge')
        self.progress.publication = {**(self.progress.publication or {}), **result}
        if isinstance(merge, dict):
            self._base_merge_record(merge)
        self.progress.publication_status = 'completed'
        self._pushed = True
        self._base_merged = str(result.get('commit') or '')
        workflow.logger.info('Base branch merged into %s at %s', result.get('branch'),
                             self._base_merged)

    async def _base_merge_finished(self, fix: dict[str, Any]) -> None:
        """Say in the pull request, and in the progress, how this run's base merge round
        ended: the merge commit pushed, or what failed."""
        base = self._base_name()
        publication = self.progress.publication or {}
        if self._base_merged:
            self._base_merge_update(status='completed', commit=self._base_merged)
            await self._ci_comment(
                f'Automatic fix round {fix["round"]} of {MAX_MERGE_FIX_ROUNDS} merged `{base}` '
                f'(at `{fix["commit"][:12]}`) into `{publication.get("branch", "")}` and '
                f'pushed the merge commit `{self._base_merged[:12]}`.')
            return
        error = (self._base_merge_error or self.progress.documentation_error
                 or self.progress.implementation_error
                 or self.progress.tests_error or self.progress.component_tests_error
                 or self.progress.baseline_error
                 or self.progress.verification_error or self.progress.review_error
                 or self.progress.publication_error
                 or self._unpushed_reason())
        self._base_merge_update(status='error', error=error)
        await self._ci_comment(
            f'Automatic fix round {fix["round"]} of {MAX_MERGE_FIX_ROUNDS} for the conflicts '
            f'with `{base}` ended without a push of the merge: {error}')
