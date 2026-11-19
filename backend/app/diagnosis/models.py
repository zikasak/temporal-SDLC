"""Bounded verdict and repair payloads exchanged between verification and authoring."""
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

from app.verification.models import TestRef

RepairTarget = Literal['implementation', 'tests']
TARGETS = ('implementation', 'tests')
# Which suite a repair of the tests of a change with component tests corrects: its HTTP
# contract tests or its component tests. Each has its own author, and only that one runs.
RepairSuite = Literal['contract', 'component']
SUITES = ('contract', 'component')
SUMMARY_CHARS = 1000
INSTRUCTION_CHARS = 6000
# Every model invocation of the diagnosing and repairing agents repeats these.
REPAIR_FAILURES = 30
FAILURE_MESSAGE_CHARS = 600


class Diagnosis(TypedDict):
    session_id: str
    target: RepairTarget
    # The failing tests that are themselves wrong: the only ones a repair of the tests
    # may change. Empty when the implementation is at fault.
    tests: list[TestRef]
    summary: str
    instructions: str
    path: str
    # The suite `tests` belong to, for a change with component tests whose tests are at
    # fault; absent otherwise.
    suite: NotRequired[RepairSuite]


class RepairRound(TypedDict):
    attempt: int
    target: RepairTarget
    tests: list[TestRef]
    summary: str
    suite: NotRequired[RepairSuite]


def failed_tests(*verifications: Mapping[str, Any] | None) -> dict[str, Any]:
    """The failing tests of verification results, clipped for a model's context: of one
    run, or of a change's contract and component test runs together."""
    runs = [run for run in verifications if run]
    failures = [item for run in runs for item in run.get('test_failures') or []]
    return {
        'test_failures': [{**item, 'message': item['message'][:FAILURE_MESSAGE_CHARS]}
                          for item in failures[:REPAIR_FAILURES]],
        'failing_tests': sum(run.get('failures', 0) + run.get('errors', 0) for run in runs),
    }


def repair_request(diagnosis: dict[str, Any], verification: Mapping[str, Any] | None,
                   attempt: int, component: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What the author of the faulty side receives; never the other side's sources.
    `component` is the run of a change's component tests, beside `verification`, the run
    of its contract tests, which is None when it has none: the implementation's author
    gets the failures of both, a test author those of its own suite alone."""
    if diagnosis['target'] == 'implementation':
        request = {'attempt': attempt, 'instructions': diagnosis['instructions'],
                   **failed_tests(verification, component)}
        # The test author works from the contract alone, so only the service sees its log.
        request['service_log'] = (verification or {}).get('service_log', '')
        return request
    run = component if diagnosis.get('suite') == 'component' else verification
    return {'attempt': attempt, 'instructions': diagnosis['instructions'],
            **failed_tests(run), 'broken_tests': diagnosis['tests']}
