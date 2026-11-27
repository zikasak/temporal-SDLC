"""Sign-in and the API-wide authentication requirement, through the real application."""

import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.api.main import app
from app.api.routes import auth as auth_routes
from app.auth import limits, sessions, storage, tokens
from app.auth.passwords import hash_password
from app.github import storage as github_storage

PASSWORD = "correct horse battery"
PUBLIC = {("POST", "/auth/login"), ("POST", "/auth/logout"), ("GET", "/auth/registration"),
          ("POST", "/auth/register"), ("POST", "/auth/refresh")}


@pytest.fixture
def http(monkeypatch):
    use_accounts(monkeypatch, {"alice@example.com": {
        "id": 7, "login": "alice@example.com", "password_hash": hash_password(PASSWORD),
        "created_at": "2026-01-01T00:00:00Z"}})
    monkeypatch.setattr(tokens, "AUTH_JWT_SECRET", "s" * 32)
    monkeypatch.setattr(auth_routes, "login_attempts", limits.AttemptLimiter(10))
    monkeypatch.setattr(auth_routes, "refresh_attempts", limits.AttemptLimiter(30))
    # No GitHub connection: webhook deliveries cannot be signed with its secret.
    monkeypatch.setattr(github_storage, "get_connection", no_connection)
    # No lifespan: these requests never reach PostgreSQL or Temporal.
    return TestClient(app)


async def no_connection():
    return None


def use_accounts(monkeypatch, users):
    """Replace the accounts table with `users`, keyed by login."""
    async def get_user(login):
        return users.get(login)

    async def first_user_id():
        return min((user["id"] for user in users.values()), default=None)

    async def create_user(login, password_hash, *, only_first=False):
        if login in users or (only_first and users):
            return None
        users[login] = {"id": 100 + len(users), "login": login, "password_hash": password_hash,
                        "created_at": "2026-01-02T00:00:00Z"}
        return users[login]

    async def list_users():
        return sorted(users.values(), key=lambda user: user["login"])

    for function in (get_user, first_user_id, create_user, list_users):
        monkeypatch.setattr(storage, function.__name__, function)
    return use_sessions(monkeypatch, users)


def use_sessions(monkeypatch, users):
    """Replace the session tables with dictionaries, with the storage's semantics."""
    table, refresh_tokens = {}, {}
    clock = {"now": datetime.now(timezone.utc)}

    async def start_session(user_id):
        session_id, token = f"session-{len(table)}", sessions.new_refresh_token()
        table[session_id] = {"user_id": user_id, "created_at": clock["now"],
                             "last_active_at": clock["now"], "revoked": False}
        refresh_tokens[token] = {"session_id": session_id, "replaced_at": None}
        return session_id, token

    async def touch_session(session_id, user_id):
        session = table.get(session_id)
        if not session or session["revoked"] or session["user_id"] != user_id:
            return False
        session["last_active_at"] = clock["now"]
        return True

    async def revoke_session(session_id):
        if session_id in table:
            table[session_id]["revoked"] = True

    async def session_of_refresh_token(token):
        entry = refresh_tokens.get(token)
        return entry["session_id"] if entry else None

    async def rotate(token):
        entry = refresh_tokens.get(token)
        if entry is None:
            raise sessions.RefreshRejected("invalid")
        session, now = table[entry["session_id"]], clock["now"]
        if session["revoked"]:
            raise sessions.RefreshRejected("revoked")
        if now - session["created_at"] > timedelta(days=30):
            raise sessions.RefreshRejected("expired")
        if now - session["last_active_at"] > timedelta(days=7):
            raise sessions.RefreshRejected("idle")
        replaced = entry["replaced_at"]
        if replaced is not None and now - replaced > timedelta(
                seconds=sessions.REFRESH_GRACE_SECONDS):
            session["revoked"] = True
            raise sessions.RefreshRejected("revoked")
        for other in refresh_tokens.values():
            if other["session_id"] == entry["session_id"] and other["replaced_at"] is None:
                other["replaced_at"] = now
        new_token = sessions.new_refresh_token()
        refresh_tokens[new_token] = {"session_id": entry["session_id"], "replaced_at": None}
        session["last_active_at"] = now
        user = next(user for user in users.values() if user["id"] == session["user_id"])
        return ({"session_id": entry["session_id"], "id": user["id"], "login": user["login"],
                 "created_at": session["created_at"]}, new_token)

    for function in (start_session, touch_session, revoke_session, session_of_refresh_token,
                     rotate):
        monkeypatch.setattr(sessions, function.__name__, function)
    return clock


def sign_in(http, login="alice@example.com", password=PASSWORD):
    return http.post("/auth/login", json={"login": login, "password": password})


def test_every_route_but_sign_in_requires_authentication(http):
    routes = {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
              for method in operations}
    assert len(routes) > len(PUBLIC)
    for method, path in routes - PUBLIC:
        response = http.request(method, path)
        assert response.status_code == 401, f"{method} {path} is reachable without signing in"


@pytest.fixture
def clock(monkeypatch):
    """Accounts with alice and the session tables' clock, which tests move forward."""
    clock = use_accounts(monkeypatch, {"alice@example.com": {
        "id": 7, "login": "alice@example.com", "password_hash": hash_password(PASSWORD),
        "created_at": "2026-01-01T00:00:00Z"}})
    return clock


def refresh(http, token=None):
    return http.post("/auth/refresh", json={"refresh_token": token} if token else {})


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
    unsigned = jwt.encode(claims, "", algorithm="none")
    monkeypatch.setattr(tokens, "AUTH_ACCESS_TOKEN_TTL_MINUTES", -1)
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


def test_the_first_account_registers_itself_and_then_registration_closes(http, monkeypatch):
    use_accounts(monkeypatch, {})
    assert http.get("/auth/registration").json() == {"open": True}
    weak = http.post("/auth/register", json={"login": "bob@example.com", "password": "short"})
    assert weak.status_code == 422 and "short" not in weak.text
    assert http.get("/auth/registration").json() == {"open": True}

    first = http.post("/auth/register", json={"login": " Bob@Example.com", "password": PASSWORD})
    assert first.status_code == 201
    assert first.json()["login"] == "bob@example.com" and first.json()["admin"] is True
    assert http.get("/auth/me").json()["admin"] is True

    http.cookies.clear()
    assert http.get("/auth/registration").json() == {"open": False}
    late = http.post("/auth/register", json={"login": "eve@example.com", "password": PASSWORD})
    assert late.status_code == 403
    assert sign_in(http, login="eve@example.com").status_code == 401
    assert sign_in(http, login="bob@example.com").json()["admin"] is True


def test_only_the_first_user_registers_the_others(http):
    carol = {"login": "Carol@Example.com", "password": "carol's own password"}
    assert sign_in(http).json()["admin"] is True
    created = http.post("/auth/users", json=carol)
    assert created.status_code == 201 and created.json()["login"] == "carol@example.com"
    assert PASSWORD not in created.text and "password_hash" not in created.text
    assert http.post("/auth/users", json=carol).status_code == 409
    assert http.post("/auth/users", json={**carol, "login": "no spaces"}).status_code == 422
    assert [user["login"] for user in http.get("/auth/users").json()] == [
        "alice@example.com", "carol@example.com"]

    http.cookies.clear()
    signed_in = sign_in(http, login="carol@example.com", password=carol["password"])
    assert signed_in.json()["admin"] is False
    dave = {"login": "dave@example.com", "password": PASSWORD}
    assert http.post("/auth/users", json=dave).status_code == 403
    assert http.get("/auth/users").status_code == 403
    # A token is not enough on its own: the claim is checked against the accounts.
    session_id = jwt.decode(signed_in.json()["access_token"], "s" * 32,
                            algorithms=["HS256"])["sid"]
    forged = jwt.encode({"sub": "101", "login": "carol@example.com", "admin": True,
                         "sid": session_id, "exp": 4102444800}, "s" * 32, algorithm="HS256")
    http.cookies.clear()
    assert http.post("/auth/users", json=dave,
                     headers={"Authorization": f"Bearer {forged}"}).status_code == 403


def test_sign_in_returns_a_refresh_token_in_the_body_and_a_scoped_cookie(http):
    response = sign_in(http)
    body = response.json()
    assert body["refresh_token"] and body["token_type"] == "bearer"
    cookies = [value.lower() for key, value in response.headers.multi_items()
               if key == "set-cookie"]
    refresh_cookie = next(cookie for cookie in cookies if cookie.startswith("refresh_token="))
    assert "httponly" in refresh_cookie and "samesite=strict" in refresh_cookie
    assert "path=/api/auth/refresh" in refresh_cookie
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    lifetime = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < lifetime <= timedelta(minutes=15)


def test_refresh_rotates_the_token_and_rejects_reuse_after_the_grace_period(http, clock):
    first = sign_in(http).json()["refresh_token"]
    http.cookies.clear()
    renewed = refresh(http, first)
    assert renewed.status_code == 200
    second = renewed.json()["refresh_token"]
    assert second != first and renewed.json()["login"] == "alice@example.com"
    bearer = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
    http.cookies.clear()
    assert http.get("/auth/me", headers=bearer).status_code == 200
    # A replaced token still works within the grace period...
    assert refresh(http, first).status_code == 200
    clock["now"] += timedelta(seconds=sessions.REFRESH_GRACE_SECONDS + 1)
    # ...and afterwards its reuse revokes the whole session.
    reused = refresh(http, first)
    assert reused.status_code == 401 and reused.json()["reason"] == "revoked"
    assert reused.headers["www-authenticate"] == "Bearer"
    http.cookies.clear()
    assert http.get("/auth/me", headers=bearer).status_code == 401


def test_refresh_rejects_missing_unknown_idle_and_expired_sessions(http, clock):
    assert refresh(http).json()["reason"] == "invalid"
    assert refresh(http, "unknown").json()["reason"] == "invalid"
    idle = sign_in(http).json()["refresh_token"]
    clock["now"] += timedelta(days=8)
    response = refresh(http, idle)
    assert response.status_code == 401 and response.json()["reason"] == "idle"

    token = sign_in(http).json()["refresh_token"]
    for _ in range(6):  # active every few days, yet past the absolute limit
        clock["now"] += timedelta(days=5)
        renewed = refresh(http, token)
        assert renewed.status_code == 200
        token = renewed.json()["refresh_token"]
    clock["now"] += timedelta(days=1)
    assert refresh(http, token).json()["reason"] == "expired"


def test_sign_out_revokes_the_session_of_a_bearer_token_at_once(http, clock):
    session = sign_in(http).json()
    http.cookies.clear()
    bearer = {"Authorization": f"Bearer {session['access_token']}"}
    assert http.get("/auth/me", headers=bearer).status_code == 200
    assert http.post("/auth/logout", headers=bearer, json={}).status_code == 204
    assert http.get("/auth/me", headers=bearer).status_code == 401
    assert refresh(http, session["refresh_token"]).json()["reason"] == "revoked"
    # An API client without its access token signs out with the refresh token.
    other = sign_in(http).json()
    http.cookies.clear()
    assert http.post("/auth/logout", json={"refresh_token": other["refresh_token"]}
                     ).status_code == 204
    assert refresh(http, other["refresh_token"]).json()["reason"] == "revoked"


def test_tokens_without_a_session_id_are_rejected(http):
    old = jwt.encode({"sub": "7", "login": "alice@example.com", "exp": 4102444800},
                     "s" * 32, algorithm="HS256")
    response = http.get("/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


def test_refresh_attempts_are_limited_separately_from_sign_in(http, monkeypatch):
    monkeypatch.setattr(auth_routes, "refresh_attempts", limits.AttemptLimiter(3))
    assert {refresh(http, "unknown").status_code for _ in range(3)} == {401}
    limited = refresh(http, "unknown")
    assert limited.status_code == 429 and 0 < int(limited.headers["retry-after"]) <= 60
    assert sign_in(http).status_code == 200


def test_refresh_without_a_signing_secret_is_unavailable(http, monkeypatch):
    token = sign_in(http).json()["refresh_token"]
    monkeypatch.setattr(tokens, "AUTH_JWT_SECRET", "")
    assert refresh(http, token).status_code == 503
