"""What the pull request of a pushed delivery says about it.

The title and the summary are written by an LLM from the task, its requirements and the
delivery's code diff (app.delivery.pull_request_activities). When it cannot write them,
the generic title of the task kind and a fixed sentence of what the delivery changed are
used instead. Either way the description is the summary, followed by which suite proves
each criterion of a routed change and by the open review findings when the delivery has
any, and the title of an unverified delivery carries the " (unverified)" marker."""
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


# The suites a routed change's criteria are proved by, in the order they are listed.
SUITES = {
    'contract': 'Contract tests (`contract-tests/`), over HTTP against the service',
    'component': ('Component tests (`component-tests/`), in-process at seams of the code '
                  'whose stubs are in `contracts/components/`'),
}
# The component suite's heading when criteria are proved at seams of other parts of the
# repository than the service, such as its web client: each part's tests and stubs.
PART_COMPONENTS = ('Component tests, in-process at seams of the code: {places}')
# Parts named in the heading; the rest are counted.
LISTED_PARTS = 10
# Criteria listed per suite; the rest are counted.
LISTED_CRITERIA = 50


def _heading(suite: str, criteria: list[dict[str, Any]]) -> str:
    """The heading of a suite's criteria. The component suite's names where each part's
    tests and stubs are when a criterion is proved at a seam of another part."""
    parts = list(dict.fromkeys(str((item.get('seam') or {}).get('part') or '')
                               for item in criteria))
    if suite != 'component' or parts == ['']:
        return SUITES[suite]
    places = [('the service\'s in `component-tests/`, with stubs in `contracts/components/`'
               if not part else
               f"part `{_line(part, 100)}`'s in `{_line(part, 100)}/component-tests/`, with "
               f"stubs in `contracts/parts/{_line(part, 100)}/components/`")
              for part in sorted(parts, key=lambda part: (part != '', part))[:LISTED_PARTS]]
    if len(parts) > LISTED_PARTS:
        places.append(f'and {len(parts) - LISTED_PARTS} more parts')
    return PART_COMPONENTS.format(places='; '.join(places))


def _proofs(change: dict[str, Any]) -> list[str]:
    """Which suite proves each criterion of a routed change (`change['proofs']`: by suite,
    the criteria with their ID, title and, at a seam, its part when it is not the
    service's, module, symbols and those of them the change added), as a part of the
    description."""
    proofs = change.get('proofs') or {}
    lines: list[str] = []
    for suite in SUITES:
        criteria = list(proofs.get(suite) or [])
        if not criteria:
            continue
        lines += ['', f'{_heading(suite, criteria)}:', '']
        for item in criteria[:LISTED_CRITERIA]:
            line = f"- {_line(item.get('id') or '', 40)}"
            if item.get('title'):
                line += f": {_line(item['title'], 200)}"
            seam = item.get('seam') or {}
            if seam.get('module'):
                symbols = ', '.join(f'`{_line(symbol, 100)}`'
                                    for symbol in list(seam.get('symbols') or [])[:10])
                added = ', '.join(f'`{_line(symbol, 100)}`'
                                  for symbol in list(seam.get('added') or [])[:10])
                part = f"{_line(seam['part'], 100)}: " if seam.get('part') else ''
                line += f" (at `{part}{_line(seam['module'], 200)}`" + (
                    f": {symbols}" if symbols else "") + (
                    f"; this change adds {added}" if added else "") + ")"
            lines.append(line)
        if len(criteria) > LISTED_CRITERIA:
            lines.append(f'- and {len(criteria) - LISTED_CRITERIA} more')
    return ['', '## How the criteria are proved', *lines] if lines else []


def describe(subject: str, change: dict[str, Any], publication: dict[str, Any],
             text: dict[str, Any] | None = None) -> tuple[str, str]:
    """The title and the description of the pull request of `publication`. `text` is the
    title and summary the LLM wrote; without it, the generic `subject` of the task kind
    and the fixed sentence of what the delivery changed are used. `change` holds the
    task's kind."""
    kind = change.get('kind') or 'new_service'
    verified = bool(publication.get('verified'))
    findings = [*_proofs(change), *_findings(publication)]
    # The proofs and the listed findings always fit whole: the summary gets what remains
    # of the body.
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
