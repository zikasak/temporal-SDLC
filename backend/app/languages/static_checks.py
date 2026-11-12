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
