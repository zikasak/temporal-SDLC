"""Session tokens, account detail bounds, delivery stage retries and request bounds that
the service answers without Temporal, an LLM provider or GitHub behind it."""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest

from conftest import PASSWORD, new_client


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unique(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# Session tokens (NFR-3, FR-3)

def test_session_token_is_an_hs256_jwt_expiring_in_the_future(admin_session):
    header_part, payload_part, signature = admin_session["access_token"].split(".")
    assert json.loads(b64url_decode(header_part))["alg"] == "HS256"
    assert signature
    expires_at = datetime.fromisoformat(admin_session["expires_at"].replace("Z", "+00:00"))
    assert expires_at.tzinfo is not None
    assert expires_at > datetime.now(timezone.utc)
    assert "exp" in json.loads(b64url_decode(payload_part))


def test_token_signed_with_another_secret_is_rejected(admin_session, base_url):
    header_part, payload_part, _ = admin_session["access_token"].split(".")
    signing_input = f"{header_part}.{payload_part}".encode()
    forged_signature = hmac.new(b"x" * 48, signing_input, hashlib.sha256).digest()
    forged = f"{header_part}.{payload_part}.{b64url_encode(forged_signature)}"
    with new_client(base_url, forged) as client:
        response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_with_a_tampered_expiry_is_rejected(admin_session, base_url):
    header_part, payload_part, signature = admin_session["access_token"].split(".")
    payload = json.loads(b64url_decode(payload_part))
    payload["exp"] = int(payload["exp"]) + 365 * 24 * 3600
    tampered = f"{header_part}.{b64url_encode(json.dumps(payload).encode())}.{signature}"
    with new_client(base_url, tampered) as client:
        assert client.get("/auth/me").status_code == 401


def test_expired_unsigned_token_is_rejected(admin_session, base_url):
    _, payload_part, _ = admin_session["access_token"].split(".")
    payload = json.loads(b64url_decode(payload_part))
    payload["exp"] = 1
    header = b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    token = f"{header}.{b64url_encode(json.dumps(payload).encode())}."
    with new_client(base_url, token) as client:
        response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_session_cookie_is_rejected(base_url):
    with new_client(base_url) as client:
        client.cookies.set("access_token", "not-a-token")
        response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_logout_while_signed_in_clears_the_cookie(admin):
    response = admin.post("/auth/logout")
    assert response.status_code == 204
    assert response.content == b""
    cookies = [c for c in response.headers.get_list("set-cookie")
               if c.strip().lower().startswith("access_token=")]
    assert cookies, response.headers.get_list("set-cookie")
    assert any(_clears_cookie(c) for c in cookies), cookies


def _clears_cookie(set_cookie: str) -> bool:
    """A Set-Cookie clears the cookie with an empty value, Max-Age of 0 or less, or an
    Expires date in the past."""
    from email.utils import parsedate_to_datetime

    first, *attributes = [part.strip() for part in set_cookie.split(";")]
    value = first.split("=", 1)[1].strip().strip('"')
    if value == "":
        return True
    for attribute in attributes:
        name, _, attr_value = attribute.partition("=")
        name = name.strip().lower()
        attr_value = attr_value.strip()
        if name == "max-age":
            try:
                if int(attr_value) <= 0:
                    return True
            except ValueError:
                pass
        elif name == "expires":
            try:
                expires = parsedate_to_datetime(attr_value)
            except (TypeError, ValueError):
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                return True
    return False


# Account details (FR-6, FR-7, NFR-2)

def test_email_login_is_accepted_and_listed_without_secrets(admin):
    login = f"{unique('mail')}@example.com"
    created = admin.post("/auth/users", json={"login": login, "password": PASSWORD})
    assert created.status_code == 201, created.text
    assert created.json()["login"] == login
    assert PASSWORD not in created.text
    listing = admin.get("/auth/users")
    assert listing.status_code == 200
    accounts = {item["login"]: item for item in listing.json()}
    assert accounts[login]["created_at"] == created.json()["created_at"]
    assert PASSWORD not in listing.text


def test_username_with_every_allowed_character_is_accepted(admin):
    login = f"a.b_c%d+e-{uuid.uuid4().hex[:10]}"
    response = admin.post("/auth/users", json={"login": login, "password": PASSWORD})
    assert response.status_code == 201, response.text
    assert response.json()["login"] == login


def test_login_and_password_at_their_maximum_lengths_are_accepted(admin, base_url):
    login = (uuid.uuid4().hex * 8)[:254]
    password = "p" * 1024
    created = admin.post("/auth/users", json={"login": login, "password": password})
    assert created.status_code == 201, created.text
    assert password not in created.text
    with new_client(base_url) as client:
        session = client.post("/auth/login", json={"login": login, "password": password})
    assert session.status_code == 200, session.text
    assert session.json()["login"] == login and session.json()["admin"] is False


def test_password_of_eight_characters_is_accepted(admin):
    response = admin.post("/auth/users", json={"login": unique(), "password": "eight-ch"})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("body", (
    {"login": (uuid.uuid4().hex * 8)[:255], "password": PASSWORD},
    {"login": "user!name", "password": PASSWORD},
    {"login": "user/name", "password": PASSWORD},
    {"login": "", "password": PASSWORD},
    {"login": None, "password": "seven-c"},
    {"login": None, "password": "q" * 1025},
), ids=("login-255", "bang", "slash", "empty-login", "password-7", "password-1025"))
def test_account_details_outside_the_bounds_are_rejected(admin, body):
    # A fresh, valid login for the password cases, so only the password is at fault.
    body = dict(body, login=body["login"] if body["login"] is not None else unique())
    response = admin.post("/auth/users", json=body)
    assert response.status_code == 422, body["login"]
    assert response.json()["detail"]
    assert body["password"] not in response.text


@pytest.mark.parametrize("body", ({"password": PASSWORD}, {"login": "someone"}),
                         ids=("no-login", "no-password"))
def test_account_creation_needs_both_credentials(admin, body):
    assert admin.post("/auth/users", json=body).status_code == 422


def test_duplicate_login_differing_in_case_conflicts(admin):
    login = unique()
    assert admin.post("/auth/users", json={"login": login,
                                           "password": PASSWORD}).status_code == 201
    response = admin.post("/auth/users", json={"login": login.upper(),
                                               "password": PASSWORD})
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)


def test_sign_in_needs_both_credentials(anonymous):
    assert anonymous.post("/auth/login", json={"login": "someone"}).status_code == 422


# Delivery stages (FR-14, FR-34, FR-36, FR-37)

SESSION_PREFIXES = ("/requirements/sessions/requirements-session-",
                    "/api-design/sessions/api-design-session-")


@pytest.mark.parametrize("prefix", SESSION_PREFIXES, ids=("requirements", "api-design"))
@pytest.mark.parametrize("stage", ("server", "tests", "documentation", "repository"))
def test_every_documented_stage_is_accepted_for_retry(admin, prefix, stage):
    response = admin.post(f"{prefix}{uuid.uuid4().hex}/generation/retry", json={"stage": stage})
    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("prefix", SESSION_PREFIXES, ids=("requirements", "api-design"))
def test_reading_an_unknown_session_requires_sign_in_first(anonymous, prefix):
    response = anonymous.get(f"{prefix}{uuid.uuid4().hex}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("prefix", SESSION_PREFIXES, ids=("requirements", "api-design"))
def test_unknown_well_formed_session_is_not_found_with_a_detail(admin, prefix):
    response = admin.get(f"{prefix}{uuid.uuid4().hex}")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


# Requirements session intake (FR-10)

VALID_SESSION = {"repository": {"full_name": "owner/repo", "branch": "main"},
                 "kind": "docs", "answers": [], "task": "Document the API."}


@pytest.mark.parametrize("missing", ("repository", "kind", "answers", "task"))
def test_requirements_session_needs_every_intake_field(admin, missing):
    body = {key: value for key, value in VALID_SESSION.items() if key != missing}
    response = admin.post("/requirements/sessions", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_requirements_session_repository_needs_a_branch(admin):
    body = dict(VALID_SESSION, repository={"full_name": "owner/repo"})
    assert admin.post("/requirements/sessions", json=body).status_code == 422


# GitHub connection input (FR-8)

@pytest.mark.parametrize("method,path", (
    ("POST", "/connectors/github/connection/check"),
    ("POST", "/connectors/github/connection/manifest"),
    ("DELETE", "/connectors/github/connection"),
), ids=("check", "manifest", "delete"))
def test_github_connection_changes_must_be_json(admin, method, path):
    response = admin.request(method, path, content="code=abc",
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415
    assert isinstance(response.json()["detail"], str)


def test_github_connection_endpoints_require_sign_in(anonymous):
    for method, path, body in (
            ("PUT", "/connectors/github/connection", {"app_id": 1, "private_key": "k"}),
            ("DELETE", "/connectors/github/connection", {}),
            ("POST", "/connectors/github/connection/check", {}),
            ("POST", "/connectors/github/connection/manifest", {"code": "abc"}),
            ("GET", "/connectors/github/repositories/owner/repo/branches", None)):
        response = anonymous.request(method, path, json=body)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer", path


# Chat input (FR-16, NFR-5)

def test_chat_request_must_be_json_or_multipart(admin):
    response = admin.post("/llm/requests", content="hello",
                          headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_chat_attachment_is_at_most_20_mib(admin):
    payload = b"a" * (20 * 1024 * 1024 + 1024)
    response = admin.post("/llm/requests", data={"message": "Summarize the file."},
                          files={"file": ("big.txt", payload, "text/plain")},
                          timeout=60.0)
    assert response.status_code in (413, 422)
    assert response.json()["detail"]


# Unavailable dependencies (NFR-4)

def test_listing_workflows_reports_unavailable_temporal(admin):
    response = admin.get("/workflows")
    assert response.status_code == 503
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]
