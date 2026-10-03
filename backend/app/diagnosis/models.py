"""Bounded verdict and repair payloads exchanged between verification and authoring."""
from typing import Any, Literal, TypedDict

RepairTarget = Literal['implementation', 'tests']
TARGETS = ('implementation', 'tests')
SUMMARY_CHARS = 1000
INSTRUCTION_CHARS = 6000
# Every model invocation of the diagnosing and repairing agents repeats these.
REPAIR_FAILURES = 30
FAILURE_MESSAGE_CHARS = 600


class Diagnosis(TypedDict):
    session_id: str
    target: RepairTarget
    summary: str
    instructions: str
    path: str


class RepairRound(TypedDict):
    attempt: int
    target: RepairTarget
    summary: str


def failed_tests(verification: dict[str, Any]) -> dict[str, Any]:
    """The failing tests of a verification result, clipped for a model's context."""
    failures = verification.get('test_failures') or []
    return {
        'test_failures': [{**item, 'message': item['message'][:FAILURE_MESSAGE_CHARS]}
                          for item in failures[:REPAIR_FAILURES]],
        'failing_tests': verification.get('failures', 0) + verification.get('errors', 0),
    }


def repair_request(diagnosis: dict[str, Any], verification: dict[str, Any],
                   attempt: int) -> dict[str, Any]:
    """What the author of the faulty side receives; never the other side's sources."""
    request = {'attempt': attempt, 'instructions': diagnosis['instructions'],
               **failed_tests(verification)}
    if diagnosis['target'] == 'implementation':
        # The test author works from the contract alone, so only the service sees its log.
        request['service_log'] = verification.get('service_log', '')
    return request
