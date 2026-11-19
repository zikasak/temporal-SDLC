"""The repository's own checks, run by verification as its CI would run them.

A repository the pipeline works on carries a checks script, CHECKS_SCRIPT, which an
onboarding writes and every implementation author may run: it installs each part's
dependencies, builds each part and runs its own unit tests, in whatever language, and
exits non-zero when anything fails. Nothing here knows the repository's CI, languages or
tools: the script is the whole interface, so the repository, its implementation and its CI
infrastructure stay independent of each other.

Verification runs the script over the repository as the delivery would push it, in the
sandbox's agent image (Python, Java with Maven, Node.js, Git), with a network but no
credentials. Its output is mostly noise for whoever has to fix a failure: dependency
downloads, passing tests, progress bars, the same traceback many times over. A failure is
cut down to what matters without the LLM (failure_excerpt), and only that excerpt is given
to the LLM to say where the problem is (analyze_checks_failure)."""

import dataclasses
import hashlib
import json
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app import docker_agents
from app.asyncio_utils import async_context, run_sync
from app.agent_workflows.host import session_language
from app.config import GENERATED_CODE_DIR
from app.github.checkout import CHECKOUT_DIR, COMPONENT_TESTS_DIR, TESTS_DIR, replace_files
from app.implementation.activities import published_files
from app.llm.limits import PROVIDER_LIMIT_ERROR, is_limit_error, retry_after
from app.temporal.tracing import trace_activity

logger = logging.getLogger(__name__)

# The repository's checks script, from its root (app/implementation/agent.py names it too).
CHECKS_SCRIPT = '.sdlc/checks.sh'
ARTIFACT_DIR = 'checks'
CHECKS_TIMEOUT = 40 * 60
# Bounds of the excerpt of a failed run and of what the LLM is given.
EXCERPT_CHARS = 12000
TAIL_LINES = 40
CONTEXT_BEFORE, CONTEXT_AFTER = 3, 12
ANALYSIS_CHARS = 4000

_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\^\[\[[0-9;]*m|\r')
_TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?Z? ?')
# Lines that say nothing about a failure, in any common toolchain.
_NOISE = re.compile(
    r'^(?:'
    r'Collecting |Downloading |Requirement already satisfied|Using cached |'
    r'Installing collected packages|Successfully (?:installed|built|uninstalled)|'
    r'Building wheels? |Created wheel|Stored in directory|Attempting uninstall|'
    r'Obtaining |Preparing metadata|Getting requirements|Installing build dependencies|'
    r'\s*Found existing installation|\s*Uninstalling |'
    r'\[notice\] |npm (?:WARN|notice) |added \d+ packages?|audited \d+ packages?|'
    r'found 0 vulnerabilities|\d+ packages? (?:are|is) looking for funding|'
    r'\s*run `npm fund`|up to date,|'
    r'\[INFO\] (?:Download|Progress|-+$|Building |BUILD SUCCESS|Total time|Finished at|'
    r'Scanning|Tests run: \d+, Failures: 0, Errors: 0)|'
    r'Download(?:ing|ed) from |Progress \(\d+\)|'
    r'.*\bPASS(?:ED)?\b(?!.*\bFAIL)|.*\s(?:ok|✓|√)$|\s*(?:✓|√|✔) |'
    r'Get:\d+ |Hit:\d+ |Fetched \d+|Reading package lists|'
    r'\s*\|?[█▉▊▋▌▍▎▏━─╸╺]{5,}'
    r')')
# A pytest progress line: a test file followed by its dots, letters and percentage.
_PROGRESS = re.compile(r'^(?:\S+\s+)?[.sxXEFp]+\s*(?:\[\s*\d+%\])?$')
# Lines that say what failed, in any common toolchain.
_SIGNAL = re.compile(
    r'\b(?:FAIL(?:ED|URE|URES)?|ERROR|Error|error(?:\[|:|\b)|Exception|Traceback|'
    r'AssertionError|assert(?:ion)?\b|panic|fatal|FATAL|Caused by|'
    r'BUILD FAILURE|COMPILATION ERROR|cannot find symbol|not found|undefined|'
    r'Segmentation fault|Killed|exit (?:code|status) [1-9])|'
    r'^E\s|^>\s|npm ERR!|^\s*(?:✗|✕|×|✘) |^\[ERROR\]|error TS\d+|'
    r'Tests run: \d+, Failures: [1-9]|Tests run: \d+, Failures: \d+, Errors: [1-9]|'
    r'short test summary info|=+ (?:FAILURES|ERRORS) =+|^\S+\.\w+:\d+(?::\d+)?:')
# The lines that state an error itself, not where it was raised from.
_STATEMENT = re.compile(r'^E\s|\b\w*(?:Error|Exception|Failure)\b:|\berror(?:\[\w+\])?:|'
                        r'^\[ERROR\]|npm ERR!|panic:|fatal:', re.IGNORECASE)
_REPEATED = '… (the line above repeated'
# A section header naming one test: `____ ERROR at setup of test_x ____`, `--- FAIL: TestX`.
_HEADER = re.compile(r'^(?:[_=-]{2,} .* [_=-]{2,}|--- FAIL: \S+.*)$')
# What makes two failures the same: addresses, durations, counts and temporary paths.
_VOLATILE = re.compile(r'\d{4}-\d\d-\d\d[T ][\d:.,]+Z?|0x[0-9a-fA-F]+|'
                       r'\d+(?:\.\d+)?\s?(?:ms|s)\b|/tmp/\S+|\b\d+\b')
# Output a test runner captured from passing setup, kept with a failure's report: pytest's
# `--- Captured stdout call ---` sections, up to the next section.
_CAPTURED = re.compile(r'^-{2,} Captured \w+ \w+ -{2,}$')
_SECTION = re.compile(r'^(?:[_=-]{2,} .* [_=-]{2,}|[_=]{10,})$')


def _clean(log: str) -> list[str]:
    """The lines without colours and timestamps, each run of lines that say the same
    shown once with how often it was repeated."""
    lines: list[str] = []
    previous, repeats = None, 0
    for raw in log.splitlines():
        line = _ANSI.sub('', raw)
        stripped = line.lstrip()
        if _TIMESTAMP.match(stripped):
            line = _TIMESTAMP.sub('', stripped)
        line = line.rstrip()
        same = _VOLATILE.sub('#', line)
        if same == previous:
            repeats += 1
            continue
        if repeats:
            lines.append(f'… (the line above repeated {repeats} more times)')
        lines.append(line)
        previous, repeats = same, 0
    if repeats:
        lines.append(f'… (the line above repeated {repeats} more times)')
    return lines


def _noise(line: str) -> bool:
    text = line.strip()
    return not text or bool(_NOISE.match(text)) or bool(_PROGRESS.match(text))


def _drop_captured(lines: list[str]) -> list[str]:
    kept, captured = [], False
    for line in lines:
        if _CAPTURED.match(line.strip()):
            captured = True
            continue
        if captured and _SECTION.match(line.strip()):
            captured = False
        if not captured:
            kept.append(line)
    return kept


def failure_excerpt(log: str, limit: int = EXCERPT_CHARS) -> str:
    """What a failed run's output says about the failure, without the LLM: the lines
    around those that report errors and failures, each distinct failure once, and the end
    of the output, with dependency downloads, passing tests, progress and captured output
    left out."""
    lines = [line for line in _drop_captured(_clean(log)) if not _noise(line)]
    if not lines:
        return ''
    marked = [False] * len(lines)
    for index, line in enumerate(lines):
        if _SIGNAL.search(line):
            for near in range(max(0, index - CONTEXT_BEFORE),
                              min(len(lines), index + CONTEXT_AFTER + 1)):
                marked[near] = True
    for near in range(max(0, len(lines) - TAIL_LINES), len(lines)):
        marked[near] = True
    # Segments of kept lines, split where one is left out or a new section begins: each
    # failure a runner reports under its own header is one segment.
    segments: list[list[str]] = []
    for index, line in enumerate(lines):
        if not marked[index]:
            if segments and segments[-1]:
                segments.append([])
            continue
        if not segments or (_HEADER.match(line.strip()) and segments[-1]):
            segments.append([])
        if line.startswith(_REPEATED) and not segments[-1]:
            continue
        segments[-1].append(line)
    blocks, seen, repeated = [], set(), 0
    for block in (segment for segment in segments if segment):
        # Two failures are the same when they state the same errors, whichever test and
        # from wherever they were raised.
        said = ([_VOLATILE.sub('#', line) for line in block if _STATEMENT.search(line)]
                or [_VOLATILE.sub('#', line) for line in block
                    if _SIGNAL.search(line) and not _HEADER.match(line.strip())])
        signature = hashlib.sha1('\n'.join(said or block).encode()).hexdigest()
        if said and signature in seen:
            repeated += 1
            continue
        seen.add(signature)
        blocks.append('\n'.join(block))
    text = '\n…\n'.join(blocks)
    if repeated:
        text += f'\n… ({repeated} more failures like one shown above were left out)'
    if len(text) <= limit:
        return text
    # The start says what failed first, the end how the run ended.
    head = limit * 2 // 3
    return text[:head] + '\n… (cut) …\n' + text[-(limit - head - 15):]


def failure_signature(excerpt: str) -> list[str]:
    """The distinct error lines of an excerpt, without what varies between runs: what a
    round of fixes compares to see whether the failures changed."""
    lines = [line.strip() for line in excerpt.splitlines()]
    stated = [line for line in lines if _STATEMENT.search(line) and not _HEADER.match(line)]
    found: list[str] = []
    for line in stated or [line for line in lines
                           if _SIGNAL.search(line) and not _HEADER.match(line)]:
        # A summary line names its test before the error it states.
        normal = _VOLATILE.sub('#', re.sub(r'^\S*(?:FAILED|ERROR)\s+\S+\s+-\s+', '', line))
        if normal[:200] not in found:
            found.append(normal[:200])
    return found[:100]


# Test runners' lines naming one failed test: its identity in the first group and, where
# the line states it, the error in the second.
_FAILED_TESTS = (
    # pytest's short test summary: `FAILED tests/x.py::test_y[a] - KeyError: 'z'`, and
    # `ERROR tests/x.py - ImportError: ...` for a test file that could not be collected.
    re.compile(r'^(?:FAILED|ERROR) (\S+?(?:::\S+|\.py))(?: - (.*))?$'),
    # Go: `--- FAIL: TestRooms/lists (0.00s)`.
    re.compile(r'^\s*--- FAIL: (\S+)'),
    # Maven Surefire: `[ERROR] RoomsTest.lists -- Time elapsed: 0.01 s <<< FAILURE!`, or
    # `[ERROR] lists(RoomsTest)  Time elapsed: 0.01 s  <<< ERROR!` before Surefire 3.
    re.compile(r'^\[ERROR\] (\S+?)(?: --)?\s+Time elapsed: .*<<< (?:FAILURE|ERROR)!'),
    # Gradle: `RoomsTest > lists() FAILED`.
    re.compile(r'^(\S+ > .+?) FAILED$'),
    # Jest: `● Rooms › lists rooms`; Vitest: `FAIL  src/rooms.test.ts > Rooms > lists`.
    re.compile(r'^\s*● (.+? › .+)$'),
    re.compile(r'^\s*(?:FAIL|×|✗)\s+(\S+ > .+)$'),
)
# Runners' totals of failed tests, by runner, for when they name none of them.
_FAILED_COUNTS = {
    # `= 2 failed, 31 passed in 3.05s =`, without the rules under `pytest -q`.
    'pytest': re.compile(r'^=*\s*(?=.*\b\d+ (?:failed|errors?)\b).*\bin \d+(?:\.\d+)?s\b'),
    'surefire': re.compile(r'^\[(?:ERROR|WARNING|INFO)\] Tests run: \d+, Failures: (\d+), '
                           r'Errors: (\d+), Skipped: \d+$'),
    'jest': re.compile(r'^\s*Tests:?\s+(\d+) failed'),
    'gradle': re.compile(r'^\d+ tests? completed, (\d+) failed'),
}
# The error a test failed with, as pytest's summary states it: `pkg.mod.KeyError: ...`.
_ERROR_TYPE = re.compile(r'^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)(?::|$)')
# A traceback's lines that only chain one exception to another.
_CHAINING = re.compile(r'^(?:The above exception was the direct cause|During handling of the '
                       r'above exception)')


def _pytest_failed(line: str) -> int:
    """How many tests pytest's final line (`= 2 failed, 1 error, 31 passed in 3s =`) says
    failed or erred."""
    return sum(int(number) for number in re.findall(r'\b(\d+) (?:failed|errors?)\b', line))


def failure_outcome(log: str) -> dict[str, Any]:
    """What a failed run left failing, for the decision whether another round of fixes is
    worth it (app.delivery.rounds): `failures`, how many tests failed, and `signature`,
    which ones, each with the type of the error it failed with where the runner states it.

    Tests are what a round fixes, so they are what is counted: the length of a traceback
    says nothing about how much fails. Only output that names no failed test, such as a
    build that did not compile, is judged by its distinct error lines, with traceback
    indentation and exception chaining taken out.

    The CI's fix rounds call this from workflow code, so it has to stay deterministic, and
    a change to what it returns has to be put behind a workflow patch there."""
    lines = _clean(log)
    tests: dict[str, str] = {}
    counts: dict[str, int] = {}
    for line in lines:
        text = line.strip()
        for pattern in _FAILED_TESTS:
            match = pattern.match(text)
            if match:
                kind = _ERROR_TYPE.match((match.groups()[1:] or [None])[0] or '')
                if not tests.get(match.group(1)):
                    tests[match.group(1)] = kind.group(1) if kind else ''
                break
        for runner, pattern in _FAILED_COUNTS.items():
            match = pattern.match(text)
            if match:
                # The last total of a runner is its whole run's.
                counts[runner] = (_pytest_failed(text) if runner == 'pytest'
                                  else sum(int(number) for number in match.groups()))
    counted = sum(counts.values())
    if tests:
        return {'failures': max(len(tests), counted), 'signature': sorted(
            f'{test} ({kind})' if kind else test for test, kind in tests.items())[:100]}
    errors: list[str] = []
    for line in failure_signature('\n'.join(lines)):
        error = re.sub(r'^(?:E|>)\s+', '', line)
        if not _CHAINING.match(error) and error not in errors:
            errors.append(error)
    return {'failures': counted or len(errors) or 1, 'signature': sorted(errors)[:100]}


# A frame of the repository's own code in a pytest traceback, `tests/x.py:90: in test_y` or
# `app/x.py:12: KeyError`: a relative path, unlike the frames of installed packages.
_OWN_FRAME = re.compile(r'^[\w.-][\w./-]*\.\w+:\d+:')
# pytest's header of one failed test's section: `____ TestX.test_y[a] ____`, or `_ name _`
# for a long name; not the `_ _ _ _` between the frames of a traceback.
_TEST_SECTION = re.compile(r'^_+ (?:(?:ERROR|FAILURE) at \w+ of )?([^_\s].*?) _+$')
REPORT_CHARS = 5000
SECTION_LINES = 10


def failed_tests_report(log: str, limit: int = REPORT_CHARS) -> str:
    """Every test a run reports failed, with what it failed with, before anything else of
    the output: the test runner's own one-line verdicts and, for pytest, each failed test's
    error lines and the frames of the repository's own code that raised them. A long
    traceback through installed packages otherwise fills the excerpt, and whoever reads it
    learns neither which tests fail nor why. Empty when the output names no failed test."""
    lines = _clean(log)
    verdicts: list[str] = []
    for line in lines:
        text = line.strip()
        if any(pattern.match(text) for pattern in _FAILED_TESTS) and text not in verdicts:
            verdicts.append(text)
    if not verdicts:
        return ''
    sections: list[str] = []
    name: str | None = None
    body: list[str] = []

    def close() -> None:
        if name and body:
            sections.append('\n'.join([f'{name}:', *body[:SECTION_LINES]]))

    for line in lines:
        text = line.strip()
        header = _TEST_SECTION.match(text)
        if header or _SECTION.match(text):
            close()
            name, body = (header.group(1) if header else None), []
        elif name and ((text.startswith('E ') and text[1:].strip())
                       or _OWN_FRAME.match(text)):
            if text not in body:
                body.append(text)
    close()
    report = '\n'.join([f'Tests that failed ({len(verdicts)}):', *verdicts[:50]])
    if len(verdicts) > 50:
        report += f'\n… ({len(verdicts) - 50} more)'
    if sections:
        report += '\n\nWhat each failed with:\n' + '\n\n'.join(sections)
    if len(report) > limit:
        report = report[:limit - 8] + '\n… (cut)'
    return report


def checks_excerpt(log: str, limit: int = EXCERPT_CHARS) -> str:
    """The excerpt of a failed run of the checks: the failed tests and what each failed
    with (failed_tests_report), then the rest of what the output says about the failure
    (failure_excerpt), within `limit` characters."""
    report = failed_tests_report(log)
    if not report:
        return failure_excerpt(log, limit)
    rest = failure_excerpt(log, max(limit - len(report) - 20, 1000))
    return f'{report}\n\nThe output:\n{rest}'[:limit]


def assemble_repository(language: Any, session_dir: Path, target: Path) -> None:
    """The repository as the delivery would push it, without Git metadata: the checkout,
    with the implementation author's published tree and the contract tests' author's suite
    put in place as publication puts them."""
    checkout = session_dir / CHECKOUT_DIR
    shutil.copytree(checkout, target, symlinks=False,
                    ignore=lambda directory, names: ['.git'] if Path(directory) == checkout else [])
    service = session_dir / 'implementation/service'
    if service.is_dir():
        replace_files(service, target, published_files(language, service))
    suite = session_dir / 'tests/suite'
    if suite.is_dir():
        replace_files(suite, target / TESTS_DIR, lambda relative: True)


def _result(status: str, **fields: Any) -> dict[str, Any]:
    return {'status': status, 'exit_code': None, 'excerpt': '', 'signature': [],
            'failures': None, 'log': None, 'error': None, **fields}


@activity.defn
@trace_activity('run-repository-checks', feature='verification', input_parameter='language')
async def run_repository_checks(session_id: str, language: str) -> dict[str, Any]:
    """Activity 'run_repository_checks': run the repository's checks script over the
    repository as the delivery would push it. Returns `status`: `passed`, `failed` (with
    the `excerpt` of the output that matters, and the number of `failures` and their
    `signature` as failure_outcome reads them from the whole output), `missing` (the
    repository has no checks script), `not_applicable` (the session has no repository) or
    `error` (the run itself did not complete, such as when it timed out); and the `log`."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', session_id):
        raise ApplicationError('Invalid session ID', non_retryable=True)
    stack = session_language(language)
    session_dir = GENERATED_CODE_DIR / session_id
    if not await run_sync((session_dir / CHECKOUT_DIR).is_dir):
        return _result('not_applicable')
    token = uuid.uuid4().hex
    run_dir = session_dir / ARTIFACT_DIR / token
    await run_sync(run_dir.mkdir, parents=True)
    log_path = run_dir / 'output.log'
    async with docker_agents.keep_activity_alive("Running the repository's checks"):
        async with async_context(tempfile.TemporaryDirectory, dir=run_dir) as temporary:
            workspace = Path(temporary) / 'workspace'
            await run_sync(workspace.mkdir)
            await run_sync(assemble_repository, stack, session_dir, workspace / 'repository')
            if not await run_sync((workspace / 'repository' / CHECKS_SCRIPT).is_file):
                logger.info('The repository of session %s has no checks script', session_id)
                return _result('missing')
            image = dataclasses.replace(
                docker_agents.default_agent_image('implementation', 'service'),
                dockerfile=stack.agent_dockerfile)
            try:
                exit_code = await docker_agents.run_script(
                    'checks', session_id, workspace, f'sh {CHECKS_SCRIPT}', log_path,
                    image_build=image, workdir='repository', timeout=CHECKS_TIMEOUT)
            except Exception as exc:
                logger.error('The checks of session %s could not run: %s', session_id, exc)
                return _result('error', error=f'The checks script could not be run: '
                                              f'{str(exc)[-500:]}', log=str(log_path))
    log = await run_sync(log_path.read_text, encoding='utf-8', errors='replace')
    if exit_code is None:
        excerpt = checks_excerpt(log)
        return _result('error', excerpt=excerpt, log=str(log_path),
                       error=f'The checks script did not finish in {CHECKS_TIMEOUT // 60} '
                             'minutes.')
    if exit_code == 0:
        logger.info('The checks of session %s passed', session_id)
        return _result('passed', exit_code=0, log=str(log_path))
    excerpt = checks_excerpt(log)
    logger.info('The checks of session %s failed with exit status %s', session_id, exit_code)
    await run_sync((run_dir / 'excerpt.txt').write_text, excerpt, encoding='utf-8')
    found = failure_outcome(log)
    return _result('failed', exit_code=exit_code, excerpt=excerpt,
                   failures=found['failures'], signature=found['signature'],
                   log=str(log_path))


ANALYSIS_PROMPT = """The checks script of a repository (its builds and unit tests, as its
CI runs them) failed after a coding agent changed the code. Below is the relevant part of
the script's output: first every test that failed with what it failed with, then the rest
of the output about the failure; passing tests, downloads and repeated failures were left
out.

Say where the problem is, for the agent that will fix it. Answer with a JSON object and
nothing else: {{"cause": "code" or "environment", "summary": "<one sentence: what fails and
why>", "files": ["<path of a file to look at>", ...], "instructions": "<what to change,
concretely: which code or which test and how; say whether the code broke what a test rightly
asserts, or a test asserts what the change rightly altered>"}}.

Name every failed test in the instructions, with the command that runs just those tests, so
the agent can see them fail and pass. Give one diagnosis, the one the output supports best,
not a list of possibilities to check; when the output cannot tell two causes apart, say
which evidence the agent should look at to decide, and what to change in each case.

The cause is "environment" only when the failure is the sandbox's, not the repository's:
a program the checks call is not installed (such as `git` not found), or the network or a
package registry could not be reached, so that no change to the repository's code or tests
would fix it. Anything the repository's code, tests, build files or checks script could fix
is "code".
{findings}
{excerpt}"""

# What the analysis is told when the delivery is being corrected for code review findings.
FINDINGS_PROMPT = """
The change is being corrected for these code review findings, numbered, each about what
the contract requires of the change; they are the authority on what the change rightly
alters. A failed test that asserts the very behaviour a finding requires to change is
wrong, not the code:
{findings}
The agent that fixes the checks may change the code and the repository's own unit tests.
It may not change the tests in {fixed_dirs}: other authors own them, or none in this
delivery does. When failed tests there assert what a finding requires to change, add to
the JSON object "conflict": {{"finding": <its number>, "tests": ["<each such failed test>",
...], "reason": "<one sentence: what the tests assert and what the finding requires>"}},
and say in the instructions to keep the behaviour those tests assert, leaving that finding
unsettled for the user to decide about. Add no conflict when the agent may change the
test, nor when the tests are right: then say how to change the test or the code.
"""
FINDING_CHARS = 1000
MAX_ANALYSED_FINDINGS = 20
CONFLICT_TESTS = 20


def fallback_analysis(excerpt: str) -> dict[str, Any]:
    """What the fixing agent is told when the LLM could not analyse the failure."""
    first = next((line.strip() for line in excerpt.splitlines()
                  if _SIGNAL.search(line)), 'The checks script failed.')
    return {'cause': 'code', 'summary': first[:300], 'files': [],
            'instructions': ("The repository's checks script failed. Find the cause in its "
                             'output in `checks` and fix it; run the script again before you '
                             'finish.'),
            'analysed': False}


def findings_prompt(findings: list[dict[str, Any]] | None) -> str:
    """The code review findings the delivery is being corrected for, as the analysis is
    told them; nothing without any."""
    if not findings:
        return ''
    listed = '\n'.join(
        f'{number}. ' + (f'[{path}] ' if (path := item.get('path')) else '')
        + ' '.join(str(item.get('summary') or '').split())[:FINDING_CHARS]
        for number, item in enumerate(findings[:MAX_ANALYSED_FINDINGS], start=1))
    return FINDINGS_PROMPT.format(findings=listed,
                                  fixed_dirs=f'{TESTS_DIR}/ and {COMPONENT_TESTS_DIR}/')


def parse_conflict(value: Any, findings: int) -> dict[str, Any] | None:
    """The conflict an analysis found between failed tests and one of the `findings`
    review findings it was told, numbered from 1; None when it named none, or none of them."""
    if not isinstance(value, dict):
        return None
    number, tests = value.get('finding'), value.get('tests')
    if (not isinstance(number, int) or isinstance(number, bool)
            or not 1 <= number <= min(findings, MAX_ANALYSED_FINDINGS)):
        return None
    tests = [str(item)[:300] for item in tests[:CONFLICT_TESTS]] if isinstance(tests, list) else []
    if not tests:
        return None
    return {'finding': number, 'tests': tests,
            'reason': ' '.join(str(value.get('reason') or '').split())[:500]}


def parse_analysis(text: str, findings: int = 0) -> dict[str, Any] | None:
    match = re.search(r'\{.*\}', text or '', re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('instructions'), str):
        return None
    files = data.get('files')
    if not isinstance(files, list):
        files = []
    cause = 'environment' if data.get('cause') == 'environment' else 'code'
    conflict = parse_conflict(data.get('conflict'), findings) if cause == 'code' else None
    return {'cause': cause,
            'summary': str(data.get('summary') or '')[:500],
            'files': [str(item)[:300] for item in files[:20]],
            'instructions': data['instructions'][:ANALYSIS_CHARS], 'analysed': True,
            **({'conflict': conflict} if conflict else {})}


@activity.defn
@trace_activity('analyze-checks-failure', feature='verification', input_parameter='session_id')
async def analyze_checks_failure(session_id: str, excerpt: str,
                                 findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Activity 'analyze_checks_failure': the LLM's analysis of where a failure of the
    repository's checks is, from the excerpt of their output: its `cause` (`code`, or
    `environment` when the sandbox, not the repository, is at fault), `summary`, `files`
    and `instructions`. Told the code review `findings` the delivery is being corrected
    for, it may find failed tests no author may change that one of them contradicts: its
    `conflict` names the finding, numbered from 1, the tests and why. A provider whose
    limits are spent raises a PROVIDER_LIMIT_ERROR, for the workflow to wait and ask again;
    any other failure gives the fallback analysis."""
    from langchain_core.messages import HumanMessage
    from app.llm.graph import native_session
    from app.llm.model import get_model
    try:
        model = await get_model('diagnosis')
        async with native_session(model):
            reply = await model.ainvoke([HumanMessage(content=ANALYSIS_PROMPT.format(
                findings=findings_prompt(findings), excerpt=excerpt[:EXCERPT_CHARS]))])
    except Exception as exc:
        if is_limit_error(exc):
            raise ApplicationError('The model provider\'s limits were reached.',
                                   {'retry_after': retry_after(exc)},
                                   type=PROVIDER_LIMIT_ERROR, non_retryable=True) from None
        # The provider's own message may carry credentials; it is only logged by type.
        logger.warning('The checks failure of session %s was not analysed: %s', session_id,
                       type(exc).__name__)
        return fallback_analysis(excerpt)
    text = str(getattr(reply, 'text', None) or getattr(reply, 'content', '') or '')
    return parse_analysis(text, len(findings or [])) or fallback_analysis(excerpt)
