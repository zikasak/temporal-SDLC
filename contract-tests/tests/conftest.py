import os
import time
import uuid

import httpx
import pytest

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Long enough for the API's password rules, and new for every run.
PASSWORD = f"contract-{uuid.uuid4().hex}"


@pytest.fixture(scope="session")
def base_url() -> str:
    url = os.environ.get("SERVICE_BASE_URL", "").strip()
    if not url:
        pytest.fail("SERVICE_BASE_URL must point at the running service", pytrace=False)
    return url.rstrip("/")


def new_client(base_url: str, token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=base_url, timeout=TIMEOUT, headers=headers)


@pytest.fixture
def anonymous(base_url):
    with new_client(base_url) as client:
        yield client


# The longest Retry-After a sign-in waits for before trying once more (NFR-1: per minute).
MAX_RETRY_AFTER_SECONDS = 65


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return min(float(value) + 1, MAX_RETRY_AFTER_SECONDS)
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return MAX_RETRY_AFTER_SECONDS
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return min(max((when - datetime.now(timezone.utc)).total_seconds() + 1, 1),
               MAX_RETRY_AFTER_SECONDS)


def sign_in(base_url: str, login: str, password: str) -> httpx.Response:
    """POST /auth/login to set up a session. Sign-ins are rate limited per minute
    (NFR-1), so a 429 waits for its Retry-After once and tries again."""
    with new_client(base_url) as client:
        response = client.post("/auth/login", json={"login": login, "password": password})
        if response.status_code == 429:
            time.sleep(_retry_after_seconds(response))
            response = client.post("/auth/login", json={"login": login, "password": password})
    return response


@pytest.fixture(scope="session")
def admin_credentials(base_url) -> dict:
    """The first user's login and password, plus the Session body it was registered or
    signed in with (key "registration")."""
    with new_client(base_url) as client:
        try:
            registration = client.get("/auth/registration")
        except httpx.TransportError as exc:
            pytest.fail(f"The service at {base_url} cannot be reached: {exc}", pytrace=False)
        assert registration.status_code == 200, registration.text
        if registration.json()["open"]:
            login = f"admin-{uuid.uuid4().hex[:12]}"
            response = client.post("/auth/register", json={"login": login, "password": PASSWORD})
            assert response.status_code == 201, response.text
            return {"login": login, "password": PASSWORD, "registration": response.json()}
    login = os.environ.get("CONTRACT_TESTS_ADMIN_LOGIN")
    password = os.environ.get("CONTRACT_TESTS_ADMIN_PASSWORD")
    if not login or not password:
        pytest.fail("Registration is closed: supply CONTRACT_TESTS_ADMIN_LOGIN and "
                    "CONTRACT_TESTS_ADMIN_PASSWORD", pytrace=False)
    response = sign_in(base_url, login, password)
    assert response.status_code == 200, response.text
    return {"login": login, "password": password, "registration": response.json()}


@pytest.fixture(scope="session")
def admin_session(admin_credentials) -> dict:
    """The first user's session: registered by the suite while registration is open."""
    return dict(admin_credentials["registration"])


@pytest.fixture
def admin(base_url, admin_session, admin_credentials):
    """A client signed in as the administrator. Signing out revokes the session on the
    server (FR-241, NFR-12), so a test that signs this client out gets the shared
    session replaced by a new sign-in afterwards."""
    signed_out = []

    def note_logout(request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path.rstrip("/").endswith("/auth/logout"):
            signed_out.append(True)

    with new_client(base_url, admin_session["access_token"]) as client:
        client.event_hooks["request"].append(note_logout)
        yield client
    if signed_out:
        response = sign_in(base_url, admin_credentials["login"], admin_credentials["password"])
        assert response.status_code == 200, response.text
        admin_session.clear()
        admin_session.update(response.json())
