"""The automatic fix round of a push that commits on its remote branch conflicted with."""
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
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
