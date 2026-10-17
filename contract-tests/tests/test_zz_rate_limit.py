"""Sign-in attempt limit (NFR-1). This module is named to run last: it uses up the
client address's sign-in budget for the current minute."""
import uuid

from conftest import PASSWORD

DEFAULT_ATTEMPTS_PER_MINUTE = 10


def _is_http_date(value: str) -> bool:
    """Retry-After may also be an HTTP-date (RFC 9110)."""
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value) is not None
    except (TypeError, ValueError):
        return False


def test_too_many_sign_in_attempts_answer_429_with_retry_after(admin_session, anonymous):
    # admin_session has already signed in, so the budget is at most 10 more attempts.
    statuses = []
    for _ in range(DEFAULT_ATTEMPTS_PER_MINUTE + 1):
        response = anonymous.post("/auth/login", json={
            "login": f"nobody-{uuid.uuid4().hex[:12]}", "password": PASSWORD})
        statuses.append(response.status_code)
        if response.status_code == 429:
            break
    assert statuses[-1] == 429, statuses
    assert set(statuses[:-1]) <= {401}, statuses
    retry_after = response.headers["retry-after"].strip()
    assert retry_after.isdigit() or _is_http_date(retry_after), retry_after
    assert isinstance(response.json()["detail"], str)
    assert PASSWORD not in response.text
