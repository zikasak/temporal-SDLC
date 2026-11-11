"""The repository's CI on a delivery's pushed commit: its GitHub checks, which run what the
pipeline's verification does not, such as the repository's own unit tests, the end of the
logs of those that failed, and the comments the delivery writes about them."""

import re
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.github import client as github_client
from app.github.errors import GitHubError
from app.temporal.activities import _github_token

# The error type of a check read the GitHub App is not allowed: the delivery stops
# watching the commit's CI and says why.
CHECKS_UNAVAILABLE = 'ChecksUnavailable'
CHECKS_DENIED = ("The GitHub App may not read the commit's checks. Give it the Checks: read "
                 "and Actions: read repository permissions and accept them on its "
                 "installation for the delivery to watch its CI.")
# Conclusions of a finished check that fail the commit, and those that pass it.
FAILED = frozenset({'failure', 'timed_out', 'startup_failure', 'action_required'})
PASSED = frozenset({'success', 'skipped', 'neutral'})
# How many checks a delivery's progress keeps, and how many failed ones a report covers.
KEPT_CHECKS = 30
REPORTED_CHECKS = 4
# The characters of a failed job's log a report quotes, and of the whole report.
LOG_CHARS = 6000
REPORT_CHARS = 20000
# How much of a log's end is read to find the part that failed.
LOG_READ_CHARS = 60000
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\^\[\[[0-9;]*m')
# GitHub's own lines after the step that failed: the post-job steps and cleanup.
POST_STEPS = re.compile(r'^(?:Post job cleanup|##\[group\]Run actions/|Cleaning up orphan)')


def summarize_checks(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """How the commit's CI stands from its check runs: `status` (`pending`, `passed`,
    `failed`, `inconclusive` or `none`) and the checks, the failed ones first."""
    if not runs:
        return {'status': 'none', 'checks': []}
    finished = all(run['status'] == 'completed' for run in runs)
    failed = [run for run in runs if run['status'] == 'completed'
              and run['conclusion'] in FAILED]
    if not finished:
        status = 'pending'
    elif failed:
        status = 'failed'
    elif all(run['conclusion'] in PASSED for run in runs):
        status = 'passed'
    else:
        status = 'inconclusive'
    ordered = sorted(runs, key=lambda run: (run not in failed, run['name']))
    return {'status': status, 'checks': [
        {'id': run['id'], 'name': run['name'], 'status': run['status'],
         'conclusion': run['conclusion'], 'url': run['url'], 'app': run['app'],
         'summary': run['summary'], 'text': run['text']}
        for run in ordered[:KEPT_CHECKS]]}


@activity.defn
async def commit_checks(full_name: str, commit: str) -> dict[str, Any]:
    """Activity 'commit_checks': the GitHub checks of `commit` of repository `full_name`,
    as summarize_checks says. Raises a non-retryable CHECKS_UNAVAILABLE error when the
    GitHub App may not read them."""
    token, _ = await _github_token()
    try:
        runs = await github_client.list_check_runs(token, full_name, commit)
    except GitHubError as exc:
        activity.logger.warning('Could not read the checks of %s@%s: %s', full_name,
                                commit[:12], exc)
        if exc.status_code == 403:
            raise ApplicationError(CHECKS_DENIED, type=CHECKS_UNAVAILABLE,
                                   non_retryable=True) from exc
        # The waiting delivery reads them again at its next interval.
        raise ApplicationError(str(exc), type='ChecksError', non_retryable=True) from exc
    return summarize_checks(runs)


def log_excerpt(log: str, limit: int = LOG_CHARS) -> str:
    """The end of a failed job's log up to the step that failed, without GitHub's
    timestamps, colours and post-job steps."""
    lines = [ANSI.sub('', TIMESTAMP.sub('', line)) for line in log.splitlines()]
    errors = [index for index, line in enumerate(lines) if line.startswith('##[error]')]
    if errors:
        lines = lines[:errors[-1] + 1]
    else:
        cut = next((index for index, line in enumerate(lines) if POST_STEPS.match(line)), None)
        if cut is not None:
            lines = lines[:cut]
    text = '\n'.join(line for line in lines if line.strip())
    return text[-limit:]


async def _check_report(token: str, full_name: str, check: dict[str, Any]) -> str:
    heading = f"### {check['name']} ({check.get('conclusion') or 'failed'})"
    if check.get('url'):
        heading += f"\n{check['url']}"
    details = ''
    if check.get('app') == 'github-actions':
        # A GitHub Actions check run is its job: the job's log says what failed.
        try:
            log = await github_client.job_log_tail(token, full_name, check['id'],
                                                   LOG_READ_CHARS)
        except GitHubError as exc:
            activity.logger.warning('Could not read the log of %s in %s: %s',
                                    check['name'], full_name, exc)
            details = ('(The log could not be read'
                       + (': the GitHub App needs the Actions: read permission.)'
                          if exc.status_code == 403 else '.)'))
        else:
            excerpt = log_excerpt(log)
            details = f'The end of its log:\n```\n{excerpt}\n```' if excerpt else ''
    if not details:
        output = '\n\n'.join(item for item in (check.get('summary'), check.get('text')) if item)
        details = output or '(The check reported no details.)'
    return f'{heading}\n{details}'


@activity.defn
async def ci_failure_report(full_name: str, commit: str, checks: list[dict[str, Any]]) -> str:
    """Activity 'ci_failure_report': what failed in the CI of `commit`, from the failed
    `checks` that commit_checks returned: each one's name, link and the end of its log,
    or its own summary when it is not a GitHub Actions job."""
    token, _ = await _github_token()
    failed = [check for check in checks if check.get('conclusion') in FAILED]
    parts = [await _check_report(token, full_name, check)
             for check in failed[:REPORTED_CHECKS]]
    if len(failed) > REPORTED_CHECKS:
        parts.append('Also failed: ' + ', '.join(
            check['name'] for check in failed[REPORTED_CHECKS:]))
    return '\n\n'.join(parts)[:REPORT_CHARS]


@activity.defn
async def comment_pull_request(full_name: str, number: int, text: str) -> str:
    """Activity 'comment_pull_request': a conversation comment of the delivery's own on
    pull request `number` of `full_name`, such as about its CI; returns its link."""
    token, _ = await _github_token()
    try:
        return await github_client.comment_on_pull_request(token, full_name, number, text)
    except GitHubError as exc:
        activity.logger.warning('Could not comment in pull request #%d of %s: %s', number,
                                full_name, exc)
        raise ApplicationError(
            str(exc), non_retryable=exc.status_code in (401, 403, 404, 409, 422)) from exc
