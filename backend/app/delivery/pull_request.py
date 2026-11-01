"""What the pull request of a pushed delivery says about it.

The title and the summary are written by an LLM from the task, its requirements and the
delivery's code diff (app.delivery.pull_request_activities). When it cannot write them,
the generic title of the task kind and a fixed sentence of what the delivery changed are
used instead. Either way the description is the summary, followed only by the open review
findings when the delivery has any, and the title of an unverified delivery carries the
" (unverified)" marker."""
import re
from typing import Any, TypedDict

from app.review.models import commit_findings

TITLE_CHARS = 200
# The LLM's summary; GitHub takes descriptions of up to 65536 characters, and the open
# findings follow it.
SUMMARY_CHARS = 20000
# The longest description the GitHub client sends (app.github.client.BODY_CHARS); the
# summary is cut so that the open findings after it are never cut off.
BODY_CHARS = 65000
UNVERIFIED_MARKER = ' (unverified)'

CHANGES = {
    'new_service': ('Adds the generated service, its contract tests in contract-tests/ and '
                    'the contract in contracts/ they were built from.'),
    'api_change': ("Changes the service's API, its contract tests and the contract in "
                   'contracts/ to the new design.'),
    'bugfix': ('Fixes the service and adds regression tests that fail against the service '
               'as the default branch holds it; the contract in contracts/ records the fix.'),
    'docs': ('Changes the documentation only; the service, its tests and its contract stay '
             'as they are.'),
    'onboarding': ('Onboards the existing service: the contract in contracts/ drafted from '
                   'its code, contract tests in contract-tests/ that pass against it and a '
                   'root Dockerfile that builds it.'),
    'onboarding_architecture': ('Adds the architecture of the existing service, drafted from '
                                'its code, to the contract: contracts/architecture.md and '
                                'contracts/architecture.json. The rest of the contract, the '
                                'contract tests and the Dockerfile stay as they are.'),
}
# Said in the description of an onboarding that changed the existing code.
CODE_CHANGED = ('The existing code was changed: the user confirmed that failing contract '
                'tests revealed bugs in it.')


class PullRequestText(TypedDict):
    """What the LLM wrote: `title` and `summary`, without the marker or the findings."""

    title: str
    summary: str


def _line(text: str, limit: int = 300) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _title(text: str, verified: bool) -> str:
    """The title, cut so that the marker of an unverified delivery always fits."""
    if verified:
        return _line(text, TITLE_CHARS)
    return _line(text, TITLE_CHARS - len(UNVERIFIED_MARKER)) + UNVERIFIED_MARKER


def _findings(publication: dict[str, Any]) -> list[str]:
    """The open review findings the pushed commit lists, as the description's last part."""
    findings = list(publication.get('findings') or [])
    count = int(publication.get('findings_count') or len(findings))
    if not findings and not count:
        return []
    # The publication keeps the first findings only; the count is of all of them.
    listed = commit_findings(findings).split('\n')[1:]
    return ['', '## Open review findings', '',
            f'Open review findings: {count}'
            + (f' (the first {len(findings)} are listed)' if count > len(findings) else ''),
            '', *listed]


def describe(subject: str, change: dict[str, Any], publication: dict[str, Any],
             text: dict[str, Any] | None = None) -> tuple[str, str]:
    """The title and the description of the pull request of `publication`. `text` is the
    title and summary the LLM wrote; without it, the generic `subject` of the task kind
    and the fixed sentence of what the delivery changed are used. `change` holds the
    task's kind."""
    kind = change.get('kind') or 'new_service'
    verified = bool(publication.get('verified'))
    findings = _findings(publication)
    # The listed findings always fit whole: the summary gets what remains of the body.
    findings_chars = sum(len(line) + 1 for line in findings)
    if text and str(text.get('title') or '').strip() and str(text.get('summary') or '').strip():
        title = _title(text['title'], verified)
        summary = str(text['summary']).strip()
        limit = max(0, min(SUMMARY_CHARS, BODY_CHARS - findings_chars - 2))
        if len(summary) > limit:
            summary = summary[:max(0, limit - 1)] + '…'
    else:
        title = _title(subject, verified)
        summary = CHANGES.get(kind, CHANGES['new_service'])
    if change.get('existing_code_changed') and CODE_CHANGED not in summary:
        summary = f'{summary}\n\n{CODE_CHANGED}'
    lines = [summary, *findings]
    return title, '\n'.join(lines) + '\n'


def parse_text(reply: str) -> PullRequestText:
    """The title and the summary from the LLM's reply: its first line, without Markdown
    heading marks or a "Title:" label, and everything after it. Raises ValueError when
    either is missing."""
    lines = reply.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise ValueError('The model wrote no title.')
    title = re.sub(r'^\s*#*\s*(\*\*)?\s*(title\s*:)?\s*', '', lines[0], flags=re.IGNORECASE)
    title = title.strip().strip('*`"\'').strip()
    summary = '\n'.join(lines[1:]).strip()
    summary = re.sub(r'^\s*(\*\*)?(description|summary)\s*:\s*(\*\*)?\s*', '', summary,
                     flags=re.IGNORECASE).strip()
    if not title:
        raise ValueError('The model wrote no title.')
    if not summary:
        raise ValueError('The model wrote no description.')
    # A title that claims the verification itself would contradict the marker.
    title = re.sub(r'\s*\(unverified\)\s*$', '', title, flags=re.IGNORECASE)
    return {'title': _line(title, TITLE_CHARS), 'summary': summary}
