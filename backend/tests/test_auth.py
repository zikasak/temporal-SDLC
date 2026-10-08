"""Sign-in and the API-wide authentication requirement, through the real application."""

import asyncio

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.api.routes import auth as auth_routes
from app.auth import limits, storage, tokens
from app.auth.passwords import hash_password

PASSWORD = "correct horse battery"
PUBLIC = {("POST", "/auth/login"), ("POST", "/auth/logout")}


@pytest.fixture
def http(monkeypatch):
    users = {"alice@example.com": {"id": 7, "login": "alice@example.com",
                                   "password_hash": hash_password(PASSWORD)}}

    async def get_user(login):
        return users.get(login)

    monkeypatch.setattr(storage, "get_user", get_user)
    monkeypatch.setattr(tokens, "AUTH_JWT_SECRET", "s" * 32)
    monkeypatch.setattr(auth_routes, "login_attempts", limits.AttemptLimiter(10))
    # No lifespan: these requests never reach PostgreSQL or Temporal.
    return TestClient(app)


def sign_in(http, login="alice@example.com", password=PASSWORD):
    return http.post("/auth/login", json={"login": login, "password": password})


def test_every_route_but_sign_in_requires_authentication(http):
    routes = {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
              for method in operations}
    assert len(routes) > len(PUBLIC)
    for method, path in routes - PUBLIC:
        response = http.request(method, path)
        assert response.status_code == 401, f"{method} {path} is reachable without signing in"


def test_email_login_issues_a_token_accepted_as_cookie_or_bearer(http):
    response = sign_in(http, login="  Alice@Example.COM ")
    assert response.status_code == 200
    assert response.json()["login"] == "alice@example.com"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert http.get("/auth/me").json()["login"] == "alice@example.com"

    token = response.json()["access_token"]
    http.cookies.clear()
    assert http.get("/auth/me").status_code == 401
    bearer = http.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert bearer.json()["login"] == "alice@example.com"


def test_wrong_password_and_unknown_login_are_indistinguishable(http):
    wrong = sign_in(http, password="incorrect horse")
    unknown = sign_in(http, login="mallory@example.com")
    malformed = sign_in(http, login="not a login")
    assert wrong.status_code == unknown.status_code == malformed.status_code == 401
    assert wrong.json() == unknown.json() == malformed.json()
    assert "set-cookie" not in wrong.headers


def test_forged_unsigned_and_expired_tokens_are_rejected(http, monkeypatch):
    claims = {"sub": "7", "login": "alice@example.com", "exp": 4102444800}
    forged = jwt.encode(claims, "another-secret-of-sufficient-length!", algorithm="HS256")
    unsigned = jwt.encode(claims, None, algorithm="none")
    monkeypatch.setattr(tokens, "AUTH_TOKEN_TTL_HOURS", -1)
    expired = sign_in(http).json()["access_token"]
    http.cookies.clear()
    for token in (forged, unsigned, expired):
        response = http.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_sign_out_ends_the_browser_session(http):
    sign_in(http)
    assert http.post("/auth/logout").status_code == 204
    assert http.get("/auth/me").status_code == 401


def test_without_a_signing_secret_nothing_is_served(http, monkeypatch):
    monkeypatch.setattr(tokens, "AUTH_JWT_SECRET", "")
    assert sign_in(http).status_code == 503
    assert http.get("/workflows", headers={"Authorization": "Bearer x"}).status_code == 503


def test_sign_in_attempts_are_limited_per_client_behind_trusted_proxies(http, monkeypatch):
    monkeypatch.setattr(auth_routes, "AUTH_TRUSTED_PROXY_HOPS", 1)

    def attempt(forwarded, password="incorrect horse"):
        return http.post("/auth/login", json={"login": "alice@example.com", "password": password},
                         headers={"X-Forwarded-For": forwarded})

    assert {attempt("203.0.113.5").status_code for _ in range(10)} == {401}
    # Even the right password waits, and a forged leading hop does not buy a new allowance.
    limited = attempt("198.51.100.1, 203.0.113.5", PASSWORD)
    assert limited.status_code == 429
    assert 0 < int(limited.headers["retry-after"]) <= 60
    assert attempt("203.0.113.6", PASSWORD).status_code == 200


async def test_a_burst_of_sign_ins_is_shed_instead_of_queued_without_bound(http, monkeypatch):
    monkeypatch.setattr(auth_routes, "login_attempts", limits.AttemptLimiter(1000))
    monkeypatch.setattr(limits, "MAX_WAITING", 2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"login": "alice@example.com", "password": "incorrect horse"}
        burst = await asyncio.gather(*(client.post("/auth/login", json=body) for _ in range(12)))
        statuses = [response.status_code for response in burst]
        assert set(statuses) == {401, 503} and statuses.count(401) >= 4
        assert all("retry-after" in response.headers for response in burst
                   if response.status_code == 503)
        recovered = await client.post("/auth/login", json={**body, "password": PASSWORD})
        assert recovered.status_code == 200


def test_passwords_are_stored_only_as_salted_hashes():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert PASSWORD not in first and first != second
