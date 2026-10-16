import os
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


@pytest.fixture(scope="session")
def admin_session(base_url) -> dict:
    """The first user's session: registered by the suite while registration is open."""
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
        else:
            login = os.environ.get("CONTRACT_TESTS_ADMIN_LOGIN")
            password = os.environ.get("CONTRACT_TESTS_ADMIN_PASSWORD")
            if not login or not password:
                pytest.fail("Registration is closed: supply CONTRACT_TESTS_ADMIN_LOGIN and "
                            "CONTRACT_TESTS_ADMIN_PASSWORD", pytrace=False)
            response = client.post("/auth/login", json={"login": login, "password": password})
            assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def admin(base_url, admin_session):
    with new_client(base_url, admin_session["access_token"]) as client:
        yield client
