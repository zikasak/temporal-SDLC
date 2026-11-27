"""Static tools the delivery review is given the results of (plans/deterministic-checks.md,
item 4). Their versions are pinned in the agent image they run in
(app/agent_workflows/Dockerfile)."""
import json
from pathlib import PurePosixPath

from app.languages import StaticCheck

# Where the trees under review are in the container the tools run in.
REVIEW_ROOT = '/workspace/review'


def _relative(value: object) -> str:
    path = str(value or '')
    for prefix in (REVIEW_ROOT + '/', './'):
        path = path.removeprefix(prefix)
    return PurePosixPath(path).as_posix() if path and not path.startswith('/') else ''


def _items(report: str) -> list:
    try:
        items = json.loads(report or '[]')
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def parse_gitleaks(report: str) -> list[dict]:
    """gitleaks' JSON report. The secret and the line it matched are never passed on."""
    return [{'rule': str(item.get('RuleID') or ''), 'path': _relative(item.get('File')),
             'line': item.get('StartLine'),
             'message': f'Possible secret: {item.get("Description") or item.get("RuleID")}'}
            for item in _items(report) if isinstance(item, dict)]


def parse_ruff(report: str) -> list[dict]:
    return [{'rule': str(item.get('code') or ''), 'path': _relative(item.get('filename')),
             'line': (item.get('location') or {}).get('row'),
             'message': str(item.get('message') or '')}
            for item in _items(report) if isinstance(item, dict)]


GITLEAKS = StaticCheck(
    name='gitleaks',
    command=('gitleaks dir . --no-banner --redact --exit-code 0 --report-format json '
             '--report-path "$REPORT"'),
    parse=parse_gitleaks)
# Errors, likely bugs and security problems only, never style. Every test asserts.
RUFF = StaticCheck(
    name='ruff',
    command=('ruff check --isolated --no-cache --select F,B,S --ignore S101 '
             '--output-format json --exit-zero . > "$REPORT"'),
    parse=parse_ruff)


def parse_eslint(report: str) -> list[dict]:
    """ESLint's JSON report. A file it could not parse (no rule) is left out, and so is
    the source text the report carries."""
    return [{'rule': str(message.get('ruleId')), 'path': _relative(item.get('filePath')),
             'line': message.get('line'), 'message': str(message.get('message') or '')}
            for item in _items(report) if isinstance(item, dict)
            for message in item.get('messages') or []
            if isinstance(message, dict) and message.get('ruleId')]


# Where the agent image installs the TypeScript check (typescript/static_check/).
ESLINT_DIR = '/opt/eslint'
# Errors and likely bugs only, such as two sibling React elements with one key, never
# style; the code under review cannot switch a rule off. ESLint exits 1 when it reports
# something and 2 when it cannot run.
ESLINT = StaticCheck(
    name='eslint',
    command=(f'{ESLINT_DIR}/node_modules/.bin/eslint --config {ESLINT_DIR}/eslint.config.mjs '
             '--format json --output-file "$REPORT" --no-error-on-unmatched-pattern '
             '--no-warn-ignored .; [ $? -le 1 ]'),
    parse=parse_eslint,
    suffixes=('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts'))
