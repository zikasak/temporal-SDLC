"""Bounded verification payloads stored in Temporal and exposed by the session API."""
from typing import Literal, TypedDict

# `reviewing` while the code review runs; `findings_open` when the tests pass but review
# findings remained after the last review round.
VerificationStatus = Literal['not_applicable', 'waiting', 'running', 'diagnosing', 'repairing',
                             'reviewing', 'completed', 'findings_open', 'skipped', 'error']


class TestFailure(TypedDict):
    class_name: str
    name: str
    kind: Literal['failure', 'error']
    message: str


class TestRef(TypedDict):
    """A test as reports name it."""
    class_name: str
    name: str


class TrackedTest(TypedDict):
    # Report entries of the test: more than one for a parameterized test, none when
    # it did not run.
    tests: int
    # How many of them failed or ended in an error.
    failed: int


class VerificationResult(TypedDict):
    session_id: str
    # `not_implemented`: this kind of task has no check yet, so nothing was run.
    status: Literal['passed', 'failed', 'error', 'not_implemented']
    tests: int
    passed: int
    failures: int
    errors: int
    skipped: int
    duration_seconds: float
    exit_code: int | None
    test_failures: list[TestFailure]
    details_truncated: bool
    # The outcome of each test the caller named, by the language's test case identity;
    # None when it named none.
    tracked: dict[str, TrackedTest] | None
    # Tests in the suite's sources that the run has no report of (the first few), when
    # the caller named tests: whatever kept them from running, they proved nothing.
    missing_tests: list[str] | None
    service_log: str
    runner_log: str
    error: str | None
    path: str
