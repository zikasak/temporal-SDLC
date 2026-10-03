"""Bounded verification payloads stored in Temporal and exposed by the session API."""
from typing import Literal, TypedDict

VerificationStatus = Literal['not_applicable', 'waiting', 'running', 'completed', 'skipped', 'error']


class TestFailure(TypedDict):
    class_name: str
    name: str
    kind: Literal['failure', 'error']
    message: str


class VerificationResult(TypedDict):
    session_id: str
    status: Literal['passed', 'failed', 'error']
    tests: int
    passed: int
    failures: int
    errors: int
    skipped: int
    duration_seconds: float
    exit_code: int | None
    test_failures: list[TestFailure]
    details_truncated: bool
    service_log: str
    runner_log: str
    error: str | None
    path: str
