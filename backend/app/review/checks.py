"""Deterministic checks the host makes before a review agent runs
(plans/deterministic-checks.md). A check may emit a finding or skip the agent only where
it is certain; whatever it cannot decide is left to the agent unchanged."""
import ast
import difflib
import re
from pathlib import Path
from typing import Any

from app.requirements.lookup import canonical_id
from app.review.models import ReviewFinding

HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
# A test names the acceptance criteria it proves in a line of its own such as
# `requirement: FR-3` or `requirements: FR-3, FR-4`.
TAG = re.compile(r'\brequirements?\s*:\s*([A-Za-z]+-\d+(?:\s*(?:,|;|\band\b)\s*[A-Za-z]+-\d+)*)',
                 re.I)
TAG_ID = re.compile(r'[A-Za-z]+-\d+')
CONTRACT_SUITE = 'tests/'
COMPONENT_SUITE = 'component-tests/'
# What each templated finding says it comes from.
TAG_REASON = 'Host check of the requirement tags of the added tests.'
SEAM_REASON = 'Host check of the seam\'s diff.'
# How many places of one criterion the reviewer is shown.
CRITERION_PLACES = 20
MAX_STATIC_FINDINGS = 100
STATIC_MESSAGE_CHARS = 300


def added_lines(diff: str) -> dict[int, str]:
    """The lines a unified diff of one file adds, by their number in the new file."""
    added: dict[int, str] = {}
    line_number: int | None = None
    for line in diff.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            line_number = int(hunk.group(1))
            continue
        if line_number is None or not line:
            continue
        if line[0] == '+':
            added[line_number] = line[1:]
            line_number += 1
        elif line[0] == ' ':
            line_number += 1
    return added


# -- Requirement tags of a change's regression tests (item 3) --------------------------

def requirement_tags(diffs: dict[str, str]) -> dict[str, list[tuple[str, int]]]:
    """The criteria the added lines of the diffs tag, each with the places tagging it."""
    tags: dict[str, list[tuple[str, int]]] = {}
    for path, diff in diffs.items():
        for number, text in added_lines(diff).items():
            for match in TAG.finditer(text):
                for item in TAG_ID.findall(match.group(1)):
                    tags.setdefault(canonical_id(item) or item, []).append((path, number))
    return tags


def _suite_tags(diffs: dict[str, str], prefix: str) -> dict[str, list[tuple[str, int]]]:
    return requirement_tags({path: diff for path, diff in diffs.items()
                             if path.startswith(prefix)})


def requirement_coverage(diffs: dict[str, str], brief_ids: list[str],
                         component_ids: list[str], contract: bool, component: bool,
                         ) -> tuple[list[ReviewFinding], dict[str, list[str]]]:
    """Findings for each criterion routed to a suite of the change that no test its authors
    added or changed tags, and for each tag naming no requirement of the brief; and the
    places that tag each criterion, as the reviewer is given them."""
    known = {canonical_id(item) or item for item in brief_ids}
    routed_to_component = {canonical_id(item) or item for item in component_ids}
    suites = ([('contract', CONTRACT_SUITE, sorted(known - routed_to_component))]
              if contract else [])
    if component:
        suites.append(('component', COMPONENT_SUITE, sorted(known & routed_to_component)))
    findings: list[ReviewFinding] = []
    places: dict[str, list[str]] = {}
    for suite, prefix, criteria in suites:
        tags = _suite_tags(diffs, prefix)
        for requirement in criteria:
            if requirement in tags:
                places.setdefault(requirement, []).extend(
                    f'{path}:{line}' for path, line in tags[requirement][:CRITERION_PLACES])
                continue
            findings.append({
                'summary': (f'No {suite} test added or changed by this change names '
                            f'acceptance criterion {requirement} of the brief. Every criterion '
                            f'routed to the {suite} tests needs a regression test that proves '
                            f'it, with a line `requirement: {requirement}` inside the test. '
                            'Add the test, or add the line to the test that proves it.'),
                'path': prefix, 'target': 'tests', 'reason': TAG_REASON})
        for requirement, tagged in sorted(tags.items()):
            if requirement in known:
                continue
            path, line = tagged[0]
            findings.append({
                'summary': (f'{path} line {line} tags requirement {requirement}, which the '
                            'brief does not have. A `requirement:` line names only the '
                            'acceptance criteria of the brief the test proves: name the '
                            'criterion this test proves, or remove the test if it proves '
                            'none.'),
                'path': path, 'target': 'tests', 'reason': TAG_REASON})
    return findings, places


# -- The seam a change adds before its tests (item 2, Python) --------------------------

_TYPING_MODULES = {'typing', 'typing_extensions', '__future__'}
_MINIMAL_RETURN = (ast.Constant, ast.Name, ast.Attribute)


def _source_lines(path: Path) -> list[str] | None:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def _inserted(before: list[str], after: list[str]) -> tuple[set[int], list[tuple[int, int]]]:
    """The lines (1-based, of `after`) inserted into `before`, and the ranges of `before`
    (1-based, inclusive) that were changed or removed. Trailing spaces are not a change."""
    old, new = [line.rstrip() for line in before], [line.rstrip() for line in after]
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    inserted: set[int] = set()
    changed: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'insert':
            inserted.update(range(j1 + 1, j2 + 1))
        elif tag != 'equal':
            changed.append((i1 + 1, max(i1 + 1, i2)))
    if changed:
        # The matcher is not a longest common subsequence: a change is certain only when
        # the old lines are not all kept, in order.
        kept = iter(enumerate(new, start=1))
        matched = {next((number for number, line in kept if line == item), 0) for item in old}
        if 0 not in matched:
            return set(range(1, len(new) + 1)) - matched, []
    return inserted, changed


def _span(node: ast.stmt) -> range:
    start = min([node.lineno, *(item.lineno for item in getattr(node, 'decorator_list', []))])
    return range(start, (node.end_lineno or node.lineno) + 1)


def _docstring(statement: ast.stmt) -> bool:
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str))


def _minimal_body(body: list[ast.stmt]) -> bool:
    """A docstring, then `pass`, `...`, `raise NotImplementedError` or a return of a
    constant, a name or an attribute; or the docstring alone."""
    rest = body[1:] if body and _docstring(body[0]) else body
    if not rest:
        return True
    if len(rest) != 1:
        return False
    statement = rest[0]
    if isinstance(statement, ast.Pass):
        return True
    if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis):
        return True
    if isinstance(statement, ast.Raise) and statement.cause is None:
        error = statement.exc
        if isinstance(error, ast.Call) and not error.keywords:
            error = error.func if all(isinstance(item, ast.Constant)
                                      for item in error.args) else None
        return isinstance(error, ast.Name) and error.id == 'NotImplementedError'
    if isinstance(statement, ast.Return):
        return statement.value is None or isinstance(statement.value, _MINIMAL_RETURN)
    return False


def _plain_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """Decorators that only mark the symbol (`@property`, `@workflow.signal`); a call such
    as `@app.get('/x')` may register it where it changes what the code does."""
    return all(isinstance(item, (ast.Name, ast.Attribute)) for item in node.decorator_list)


def _added_statement(statement: ast.stmt, names: list[str]) -> bool:
    """Whether a statement the seam added entirely is minimal; collects the names it adds."""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.append(statement.name)
        return _plain_decorators(statement) and _minimal_body(statement.body)
    if isinstance(statement, ast.ClassDef):
        names.append(statement.name)
        return (_plain_decorators(statement) and not statement.keywords
                and all(isinstance(base, (ast.Name, ast.Attribute, ast.Subscript))
                        for base in statement.bases)
                and all(_docstring(item) or isinstance(item, ast.Pass)
                        or _added_statement(item, names) for item in statement.body))
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        names.append(statement.target.id)
        return statement.value is None or isinstance(statement.value, ast.Constant)
    if isinstance(statement, ast.Assign):
        targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        names.extend(targets)
        return (len(targets) == len(statement.targets)
                and isinstance(statement.value, ast.Constant))
    if isinstance(statement, ast.ImportFrom):
        return statement.level == 0 and statement.module in _TYPING_MODULES
    if isinstance(statement, ast.Import):
        return all(alias.name in _TYPING_MODULES for alias in statement.names)
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))


def _minimal_additions(body: list[ast.stmt], lines: list[str], inserted: set[int],
                       significant: set[int], names: list[str]) -> bool:
    """Whether every statement holding a significant inserted line was added whole (each
    of its lines inserted or blank) and is minimal, or is a class whose own inserted
    statements are."""
    for statement in body:
        span = _span(statement)
        if not significant.intersection(span):
            continue
        if all(number in inserted or not lines[number - 1].strip() for number in span):
            if not _added_statement(statement, names):
                return False
        elif not (isinstance(statement, ast.ClassDef) and _minimal_additions(
                statement.body, lines, inserted, significant, names)):
            return False
    return True


def _significant(lines: list[str], inserted: set[int]) -> set[int]:
    """The inserted lines that are not blank and not only a comment."""
    return {number for number in inserted
            if lines[number - 1].strip() and not lines[number - 1].lstrip().startswith('#')}


def _ranges(ranges: list[tuple[int, int]]) -> str:
    return ', '.join(str(start) if start == end else f'{start}-{end}'
                     for start, end in ranges[:10]) + (', …' if len(ranges) > 10 else '')


def seam_check(before: Path, after: Path, diffs: dict[str, str], allowed: dict[str, str],
               build_dirs: tuple[str, ...], max_files: int = 5000,
               ) -> tuple[list[ReviewFinding], bool]:
    """The certain violations of a Python seam, and whether the seam is certainly right so
    that no agent need review it. `diffs` are the changed files under `service/`;
    `allowed` maps the repository path of each source the seam may change to its module.
    `before` and `after` are the code without and with the seam."""
    findings: list[ReviewFinding] = []
    certain = True
    names: list[str] = []
    inserted_by_path: dict[str, set[int]] = {}
    for path in diffs:
        relative = path.removeprefix('service/')
        if relative not in allowed:
            findings.append({
                'summary': (f'The seam changes {path}, which is not the source of a module '
                            'the seam lists. A seam only adds the listed symbols to their '
                            f'modules ({", ".join(sorted(set(allowed.values())))}); undo this '
                            'change.'),
                'path': path, 'target': 'implementation', 'reason': SEAM_REASON})
            continue
        old, new = _source_lines(before / relative), _source_lines(after / relative)
        if old is None or new is None:
            certain = False
            continue
        inserted, changed = _inserted(old, new)
        if changed:
            findings.append({
                'summary': (f'The seam changes or removes existing lines of {path} (lines '
                            f'{_ranges(changed)} of the code before it). A seam only adds the '
                            f'listed symbols of module {allowed[relative]}, with no behaviour '
                            'of their own, and keeps every existing line as it is: restore '
                            'those lines.'),
                'path': path, 'target': 'implementation', 'reason': SEAM_REASON})
            continue
        try:
            tree = ast.parse('\n'.join(new))
        except SyntaxError:
            certain = False
            continue
        if not _minimal_additions(tree.body, new, inserted, _significant(new, inserted),
                                  names):
            certain = False
        inserted_by_path[relative] = inserted
    if findings or not certain:
        return findings, False
    return [], not _referenced(after, names, inserted_by_path, build_dirs, max_files)


def _referenced(root: Path, names: list[str], inserted: dict[str, set[int]],
                build_dirs: tuple[str, ...], max_files: int) -> bool:
    """Whether any added name occurs in the code outside the lines that added it; true
    when that cannot be told."""
    if not names:
        return False
    pattern = re.compile(r'\b(?:' + '|'.join(sorted(map(re.escape, set(names)))) + r')\b')
    count = 0
    for file in root.rglob('*.py'):
        relative = file.relative_to(root)
        if any(part in build_dirs or part.startswith('.') for part in relative.parts[:-1]):
            continue
        count += 1
        if count > max_files or file.is_symlink():
            return True
        try:
            lines = file.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            return True
        own = inserted.get(relative.as_posix(), set())
        if any(pattern.search(line) for number, line in enumerate(lines, start=1)
               if number not in own):
            return True
    return False


# -- Static tools before the delivery review (item 4) ----------------------------------

def changed_static_findings(results: list[dict[str, Any]],
                            diffs: dict[str, str]) -> list[dict[str, Any]]:
    """The static tools' results on lines the delivery added, bounded."""
    lines = {path: added_lines(diff) for path, diff in diffs.items()}
    kept = []
    for item in results:
        path = str(item.get('path') or '')
        line = item.get('line')
        if path in lines and isinstance(line, int) and line in lines[path]:
            kept.append({'tool': str(item.get('tool') or ''), 'rule': str(item.get('rule') or ''),
                         'path': path, 'line': line,
                         'message': str(item.get('message') or '')[:STATIC_MESSAGE_CHARS]})
        if len(kept) == MAX_STATIC_FINDINGS:
            break
    return kept
