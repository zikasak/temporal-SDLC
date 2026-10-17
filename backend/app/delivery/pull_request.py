"""What the pull request of a pushed delivery says about it."""
from typing import Any

from app.requirements.models import Requirement
from app.review.models import commit_findings

# Requirements a description names one by one; the rest are counted.
LISTED_REQUIREMENTS = 50
TITLE_CHARS = 200

CHANGES = {
    'new_service': ('Adds the generated service, its contract tests in contract-tests/ and '
                    'the contract in contracts/ they were built from.'),
    'api_change': ("Changes the service's API, its contract tests and the contract in "
                   'contracts/ to the new design.'),
    'bugfix': ('Fixes the service and adds regression tests that fail against the service '
               'as the default branch holds it; the contract in contracts/ records the fix.'),
    'docs': ('Changes the documentation only; the service, its tests and its contract stay '
             'as they are.'),
}
VERIFIED = ('Verified: the contract tests pass against the built service, and the code '
            'review found nothing left to correct.')
DOCS_VERIFIED = 'Verified: the documentation passed its check.'
DOCS_UNVERIFIED = 'Not verified: no check exists for this kind of change yet.'
FINDINGS_UNVERIFIED = ('Not verified: the tests pass, but code review findings remained after '
                       'the last review round.')


def _line(text: str, limit: int = 300) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _requirements(heading: str, items: list[Requirement]) -> list[str]:
    if not items:
        return []
    lines = ['', f'### {heading}', '']
    for item in items[:LISTED_REQUIREMENTS]:
        lines.append(f'- **{_line(item.get("id", ""), 40)}** {_line(item.get("title", ""))}')
    if len(items) > LISTED_REQUIREMENTS:
        lines.append(f'- … and {len(items) - LISTED_REQUIREMENTS} more')
    return lines


def describe(subject: str, change: dict[str, Any], publication: dict[str, Any],
             session_id: str, source_branch: str) -> tuple[str, str]:
    """The title and the description of the pull request of `publication`. `change` holds
    the task's kind, the requirements it delivered and those it retired."""
    kind = change.get('kind') or 'new_service'
    verified = bool(publication.get('verified'))
    findings = list(publication.get('findings') or [])
    count = int(publication.get('findings_count') or len(findings))
    title = _line(subject if verified else f'{subject} (unverified)', TITLE_CHARS)
    lines = ['## What this delivery changed', '', CHANGES.get(kind, CHANGES['new_service'])]
    lines += _requirements('Requirements delivered', list(change.get('requirements') or []))
    lines += _requirements('Requirements retired', list(change.get('retired') or []))
    lines += ['', '## Verification', '']
    if verified:
        lines.append(DOCS_VERIFIED if kind == 'docs' else VERIFIED)
    elif findings or count:
        # The publication keeps the first findings only; the count is of all of them.
        listed = commit_findings(findings).split('\n')[1:]
        lines += [FINDINGS_UNVERIFIED, '',
                  f'Open review findings: {count}'
                  + (f' (the first {len(findings)} are listed)' if count > len(findings) else ''),
                  '', *listed]
    else:
        lines.append(DOCS_UNVERIFIED)
    lines += ['', '## Publication', '',
              f'Branch `{publication.get("branch", "")}` at commit '
              f'`{str(publication.get("commit", ""))[:40]}`, generated from '
              f'`{source_branch}` by session `{session_id}`.']
    ignored = int(publication.get('ignored_count') or 0)
    if ignored:
        names = ', '.join(f'`{_line(item, 200)}`' for item in publication.get('ignored') or [])
        lines.append(f"Left out because the repository's .gitignore matches them "
                     f'({ignored}): {names}{", …" if ignored > len(publication.get("ignored") or []) else ""}')
    return title, '\n'.join(lines) + '\n'
