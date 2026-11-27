"""Refresh attempt limit (NFR-13). Named to run after every other module: it uses up the
client address's refresh budget for the current minute. The refresh limit is separate
from the sign-in limit, which test_zz_rate_limit.py has used up just before."""
import uuid

DEFAULT_REFRESH_ATTEMPTS_PER_MINUTE = 30


def _is_http_date(value: str) -> bool:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value) is not None
    except (TypeError, ValueError):
        return False


def test_too_many_refresh_attempts_answer_429_with_retry_after(anonymous):
    token = f"unknown-{uuid.uuid4().hex}"
    statuses = []
    for _ in range(DEFAULT_REFRESH_ATTEMPTS_PER_MINUTE + 1):
        response = anonymous.post("/auth/refresh", json={"refresh_token": token})
        statuses.append(response.status_code)
        if response.status_code == 429:
            break
    # Failed attempts count too, so an unknown token reaches the limit; earlier
    # attempts of the suite may have used part of the budget already.
    # The suite makes far fewer than 30 refreshes, so the first attempt is within the
    # budget even though the sign-in budget is already used up.
    assert statuses[0] == 401, statuses
    assert statuses[-1] == 429, statuses
    assert set(statuses[:-1]) <= {401}, statuses
    retry_after = response.headers["retry-after"].strip()
    assert retry_after.isdigit() or _is_http_date(retry_after), retry_after
    assert isinstance(response.json()["detail"], str)
    assert token not in response.text
