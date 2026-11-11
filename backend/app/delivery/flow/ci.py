from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.delivery.ci_activities import CHECKS_UNAVAILABLE, FAILED
    from app.delivery.models import (
        CI_CHECK_TIMEOUT, CI_REGISTRATION_GRACE, MAX_CI_FIX_ROUNDS, CiChecks,
    )

# Marks the workflow code that watches the repository's CI on the commit a delivery
# pushed while it waits, and starts a fix round of its own when the CI fails.
CI_CHECKS_PATCH = 'ci-checks'
CI_FIX_INSTRUCTIONS = """The repository's CI failed on the commit this delivery pushed
({commit}). Its checks run what this pipeline's verification does not, such as the
repository's own unit tests and builds, so the tests you can run do not show these
failures. Change the code, or a test of this change when the test itself is wrong, so
that these checks pass. Do not delete, skip or weaken other tests to make them pass.
The failed checks, with the end of their logs:

{report}"""
CI_FIX_STARTED = ('Started automatic fix round {round} of {limit} from the failed checks\' '
                  'logs: the changes, then verification and code review. The CI of what it '
                  'pushes is reported here again.')
CI_NO_FIX = 'Mention me with what to fix to request a fix round.'


def _short(commit: str) -> str:
    return commit[:12]


class CiWatch:
    """The repository's CI on the commit the delivery pushed: watched while the delivery
    waits, reported in its pull request, and fixed by a round of its own when it fails,
    up to MAX_CI_FIX_ROUNDS times in a row."""

    def _ci_watching(self) -> bool:
        """Begin to watch the CI of the commit this run pushed, or go on watching one
        whose checks were still running when the run began; true while it is watched."""
        if not workflow.patched(CI_CHECKS_PATCH):
            return False
        publication = self.progress.publication
        if not publication or not publication.get('commit') or not self._checks_pull_request():
            return False
        if self._pushed:
            fix = self._ci_round
            if fix is None:
                # A push the user asked for starts the count of automatic rounds over.
                self.state.ci_fixes = 0
            self.progress.ci = CiChecks(
                commit=publication['commit'], status='pending',
                since=workflow.now().isoformat(), checks=[], error=None,
                fix_round=fix['round'] if fix else 0)
        return self._ci_pending()

    def _ci_pending(self) -> bool:
        ci = self.progress.ci
        publication = self.progress.publication
        return bool(ci and ci['status'] == 'pending' and publication
                    and ci['commit'] == publication.get('commit'))

    async def _check_ci(self) -> None:
        """Read the checks of the watched commit, and act on them once they finished."""
        ci = self.progress.ci
        try:
            result = await workflow.execute_activity(
                'commit_checks', args=[self.spec.repository['full_name'], ci['commit']],
                result_type=dict, start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type == CHECKS_UNAVAILABLE:
                ci['status'] = 'unavailable'
                ci['error'] = cause.message
                return
            workflow.logger.warning('CI checks not read: %s', cause or exc)
            result = None
        watched = workflow.now() - datetime.fromisoformat(ci['since'])
        status = result.get('status') if isinstance(result, dict) else 'pending'
        if isinstance(result, dict):
            ci['checks'] = [{key: check.get(key) for key in ('name', 'status', 'conclusion', 'url')}
                            for check in result.get('checks') or []]
        if status == 'none' and watched < CI_REGISTRATION_GRACE:
            # GitHub has not registered the commit's checks yet.
            status = 'pending'
        if status == 'pending' and watched >= CI_CHECK_TIMEOUT:
            status = 'timed_out'
        ci['status'] = status
        if status == 'failed':
            workflow.logger.info('CI failed on %s', _short(ci['commit']))
            await self._ci_failed(result['checks'])
        elif status == 'passed':
            workflow.logger.info('CI passed on %s', _short(ci['commit']))
            if ci['fix_round']:
                self.state.ci_fixes = 0
                await self._ci_comment(
                    f'CI passes on commit `{_short(ci["commit"])}`, pushed by automatic fix '
                    f'round {ci["fix_round"]}.')

    def _ci_fix_refused(self) -> str | None:
        """Why the failed CI is only reported, not fixed by a round of the delivery's own."""
        if self.spec.architecture_only:
            return 'Only the architecture was pushed, which the delivery does not fix on its own.'
        problem = self._fix_unavailable()
        if problem:
            return problem
        if self._next is not None or self._fix_pending() or any(
                item['status'] == 'classifying' for item in self.progress.mentions):
            return 'A fix requested in a comment runs first.'
        if self.state.ci_fixes >= MAX_CI_FIX_ROUNDS:
            return (f'{MAX_CI_FIX_ROUNDS} automatic fix rounds in a row did not make the CI '
                    'pass, so no further one starts on its own.')
        return None

    async def _ci_failed(self, checks: list[dict[str, Any]]) -> None:
        ci = self.progress.ci
        commit = ci['commit']
        failed = [check for check in checks if check['conclusion'] in FAILED]
        names = ', '.join(f"[{check['name']}]({check['url']})" if check.get('url')
                          else check['name'] for check in failed[:10])
        try:
            report = await workflow.execute_activity(
                'ci_failure_report', args=[self.spec.repository['full_name'], commit, checks],
                result_type=str, start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('CI failure not reported: %s', exc.cause or exc)
            report = 'The failed checks: ' + ', '.join(check['name'] for check in failed)
        failure = f'CI failed on commit `{_short(commit)}`: {names}.'
        refused = self._ci_fix_refused()
        if refused:
            await self._ci_comment(f'{failure} {refused} {CI_NO_FIX}')
            return
        round_ = self.state.ci_fixes + 1
        await self._ci_comment(
            f'{failure} {CI_FIX_STARTED.format(round=round_, limit=MAX_CI_FIX_ROUNDS)}')
        progress = self.progress
        progress.agent_questions = []
        progress.review_error = None
        progress.correction_failed = False
        progress.implementation_error = None
        progress.tests_error = None
        progress.component_tests_error = None
        progress.verification_status = 'repairing'
        self.state.ci_fixes = round_
        self.state.ci_fix = {'commit': commit, 'round': round_}
        self.state.instructions = CI_FIX_INSTRUCTIONS.format(commit=commit, report=report)
        self.state.waiting = False
        self.state.retry_stage = None
        self.state.record_only = False
        self._next = self.state

    async def _ci_fix_finished(self) -> None:
        """Say in the pull request when an automatic CI fix round pushed nothing; the CI
        of what it pushed is reported once its checks finish."""
        fix = self._ci_round
        self.state.ci_fix = None
        self.state.instructions = None
        if self._pushed and self.progress.publication_status == 'completed':
            return
        error = (self.progress.documentation_error or self.progress.implementation_error
                 or self.progress.tests_error or self.progress.baseline_error
                 or self.progress.verification_error or self.progress.review_error
                 or self.progress.publication_error
                 or 'The fix did not pass verification, so nothing was pushed.')
        await self._ci_comment(
            f'Automatic fix round {fix["round"]} for the CI failure on commit '
            f'`{_short(fix["commit"])}` ended without a push: {error} The branch keeps its '
            f'earlier push. {CI_NO_FIX}')

    async def _ci_comment(self, text: str) -> None:
        """Comment in the delivery's pull request; a comment that could not be posted is
        only logged, as the delivery's progress shows the same."""
        pull = self.progress.pull_request
        repository = self.spec.repository
        if not pull or not repository:
            return
        try:
            await workflow.execute_activity(
                'comment_pull_request', args=[repository['full_name'], pull['number'], text],
                result_type=str, start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('CI comment not posted: %s', exc.cause or exc)
