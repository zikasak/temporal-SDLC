"""Session renewal with refresh tokens (FR-238, FR-241, FR-242, FR-246, NFR-10, NFR-11,
NFR-12, TR-14).

Every test starts its own session by signing in as an account of this module, so that
revoking it never touches the administrator's shared session. Sign-ins are rate limited
(NFR-1); this module sorts just before test_zz_rate_limit.py, and conftest.sign_in waits
for Retry-After when the minute's budget is used up.
"""
import time
import uuid
from datetime import datetime, timezone

import pytest

from conftest import PASSWORD, new_client, sign_in

REFRESH_COOKIE_PATH = "/api/auth/refresh"
# NFR-11: a just-replaced refresh token is accepted for about 30 seconds.
PAST_GRACE_PERIOD_SECONDS = 40


@pytest.fixture(scope="module")
def member(base_url, admin_session) -> dict:
    """Credentials of a non-administrator account created through POST /auth/users."""
    login = f"renewal-{uuid.uuid4().hex[:12]}"
    with new_client(base_url, admin_session["access_token"]) as client:
        created = client.post("/auth/users", json={"login": login, "password": PASSWORD})
    assert created.status_code == 201, created.text
    return {"login": login, "password": PASSWORD}


@pytest.fixture
def signed_in(base_url, member):
    """A new session of the member: the login response."""
    response = sign_in(base_url, member["login"], member["password"])
    assert response.status_code == 200, response.text
    return response


def set_cookies(response, name: str) -> list[str]:
    return [c for c in response.headers.get_list("set-cookie")
            if c.strip().lower().startswith(f"{name}=")]


def cookie_value(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0].split("=", 1)[1].strip().strip('"')


def cookie_attributes(set_cookie: str) -> dict:
    attributes = {}
    for part in set_cookie.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.strip().lower()] = value.strip()
    return attributes


def clears(set_cookie: str) -> bool:
    """An empty value, Max-Age of 0 or less, or an Expires date in the past."""
    from email.utils import parsedate_to_datetime

    if cookie_value(set_cookie) == "":
        return True
    attributes = cookie_attributes(set_cookie)
    max_age = attributes.get("max-age")
    if max_age is not None and max_age.lstrip("-").isdigit() and int(max_age) <= 0:
        return True
    expires = attributes.get("expires")
    if expires:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when <= datetime.now(timezone.utc)
    return False


def refresh(base_url, body=None, cookie: str | None = None, bearer: str | None = None):
    headers = {}
    if cookie is not None:
        headers["Cookie"] = f"refresh_token={cookie}"
    with new_client(base_url, bearer) as client:
        return client.post("/auth/refresh", json={} if body is None else body, headers=headers)


def profile(base_url, access_token: str):
    with new_client(base_url, access_token) as client:
        return client.get("/auth/me")


def assert_session_body(session: dict, login: str) -> None:
    assert session["token_type"] == "bearer"
    assert session["login"] == login
    assert session["admin"] is False
    assert isinstance(session["access_token"], str) and session["access_token"]
    assert isinstance(session["refresh_token"], str) and session["refresh_token"]
    assert session["refresh_token"] != session["access_token"]
    expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
    assert expires_at > datetime.now(timezone.utc)


def assert_rejected(response, reasons: set[str]) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["reason"] in reasons, body


def assert_refresh_cookie(response, value: str) -> None:
    cookies = set_cookies(response, "refresh_token")
    assert cookies, response.headers.get_list("set-cookie")
    cookie = cookies[-1]
    assert cookie_value(cookie) == value
    attributes = cookie_attributes(cookie)
    assert "httponly" in attributes, cookie
    assert attributes.get("samesite", "").lower() == "strict", cookie
    assert attributes.get("path") == REFRESH_COOKIE_PATH, cookie


def test_reused_refresh_token_after_the_grace_period_revokes_the_session(base_url, signed_in):
    first = signed_in.json()["refresh_token"]
    renewed = refresh(base_url, {"refresh_token": first})
    assert renewed.status_code == 200, renewed.text
    current = renewed.json()

    time.sleep(PAST_GRACE_PERIOD_SECONDS)
    reused = refresh(base_url, {"refresh_token": first})
    assert_rejected(reused, {"revoked"})
    assert current["refresh_token"] not in reused.text and first not in reused.text

    # The whole session is revoked: the current refresh token and access token stop working.
    assert_rejected(refresh(base_url, {"refresh_token": current["refresh_token"]}), {"revoked"})
    me = profile(base_url, current["access_token"])
    assert me.status_code == 401
    assert me.headers["www-authenticate"] == "Bearer"


def test_sign_in_issues_a_refresh_token_in_the_body_and_a_scoped_cookie(signed_in, member):
    session = signed_in.json()
    assert_session_body(session, member["login"])
    assert PASSWORD not in signed_in.text
    assert_refresh_cookie(signed_in, session["refresh_token"])
    access = set_cookies(signed_in, "access_token")
    assert access and cookie_value(access[-1]) == session["access_token"]


def test_first_account_session_carries_a_refresh_token(admin_credentials):
    session = admin_credentials["registration"]
    assert isinstance(session["refresh_token"], str) and session["refresh_token"]
    assert session["refresh_token"] != session["access_token"]


def test_refresh_by_body_rotates_both_tokens(base_url, signed_in, member):
    session = signed_in.json()
    response = refresh(base_url, {"refresh_token": session["refresh_token"]})
    assert response.status_code == 200, response.text
    renewed = response.json()
    assert_session_body(renewed, member["login"])
    assert renewed["refresh_token"] != session["refresh_token"]
    assert_refresh_cookie(response, renewed["refresh_token"])
    access = set_cookies(response, "access_token")
    assert access and cookie_value(access[-1]) == renewed["access_token"]

    me = profile(base_url, renewed["access_token"])
    assert me.status_code == 200, me.text
    assert me.json()["login"] == member["login"] and me.json()["admin"] is False

    # The new refresh token renews the session again.
    again = refresh(base_url, {"refresh_token": renewed["refresh_token"]})
    assert again.status_code == 200, again.text
    assert again.json()["refresh_token"] not in (session["refresh_token"],
                                                 renewed["refresh_token"])


def test_refresh_cookie_alone_renews_and_a_body_token_wins_over_the_cookie(
        base_url, signed_in, member):
    by_cookie = refresh(base_url, cookie=signed_in.json()["refresh_token"])
    assert by_cookie.status_code == 200, by_cookie.text
    assert_session_body(by_cookie.json(), member["login"])
    current = by_cookie.json()["refresh_token"]

    # The body is used instead of the cookie, and an unusable access token is ignored.
    by_body = refresh(base_url, {"refresh_token": current},
                      cookie=f"unknown-{uuid.uuid4().hex}", bearer="not-a-token")
    assert by_body.status_code == 200, by_body.text
    assert by_body.json()["login"] == member["login"]


def test_just_replaced_refresh_token_is_accepted_within_the_grace_period(base_url, signed_in,
                                                                         member):
    first = signed_in.json()["refresh_token"]
    assert refresh(base_url, {"refresh_token": first}).status_code == 200
    again = refresh(base_url, {"refresh_token": first})
    assert again.status_code == 200, again.text
    assert again.json()["login"] == member["login"]
    assert profile(base_url, again.json()["access_token"]).status_code == 200


@pytest.mark.parametrize("body", ({}, {"refresh_token": f"unknown-{uuid.uuid4().hex}"}),
                         ids=("missing", "unknown"))
def test_missing_or_unknown_refresh_token_is_invalid(base_url, body):
    response = refresh(base_url, body)
    assert_rejected(response, {"invalid"})
    assert not set_cookies(response, "access_token") or all(
        clears(c) for c in set_cookies(response, "access_token"))


@pytest.mark.parametrize("body", ({"refresh_token": 12345}, ["token"]),
                         ids=("number", "array"))
def test_malformed_refresh_body_is_rejected(base_url, body):
    response = refresh(base_url, body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("carrier", ("bearer", "cookie"))
def test_sign_out_revokes_the_session_of_the_access_token(base_url, signed_in, carrier):
    session = signed_in.json()
    with new_client(base_url) as client:
        if carrier == "bearer":
            headers = {"Authorization": f"Bearer {session['access_token']}"}
        else:
            headers = {"Cookie": f"access_token={session['access_token']}"}
        response = client.post("/auth/logout", json={}, headers=headers)
    assert response.status_code == 204, response.text
    for name in ("access_token", "refresh_token"):
        cookies = set_cookies(response, name)
        assert cookies and any(clears(c) for c in cookies), response.headers.get_list("set-cookie")

    me = profile(base_url, session["access_token"])
    assert me.status_code == 401
    assert me.headers["www-authenticate"] == "Bearer"
    assert_rejected(refresh(base_url, {"refresh_token": session["refresh_token"]}), {"revoked"})


def test_sign_out_with_only_a_refresh_token_revokes_its_session(base_url, signed_in):
    session = signed_in.json()
    with new_client(base_url) as client:
        response = client.post("/auth/logout", json={"refresh_token": session["refresh_token"]})
    assert response.status_code == 204, response.text
    assert_rejected(refresh(base_url, {"refresh_token": session["refresh_token"]}), {"revoked"})
    assert profile(base_url, session["access_token"]).status_code == 401


def test_sign_out_ignores_the_body_when_an_access_token_is_present(base_url, member):
    first = sign_in(base_url, member["login"], member["password"])
    second = sign_in(base_url, member["login"], member["password"])
    assert first.status_code == 200 and second.status_code == 200
    first, second = first.json(), second.json()

    with new_client(base_url, first["access_token"]) as client:
        response = client.post("/auth/logout", json={"refresh_token": second["refresh_token"]})
    assert response.status_code == 204, response.text

    assert profile(base_url, first["access_token"]).status_code == 401
    assert_rejected(refresh(base_url, {"refresh_token": first["refresh_token"]}), {"revoked"})
    assert profile(base_url, second["access_token"]).status_code == 200
    kept = refresh(base_url, {"refresh_token": second["refresh_token"]})
    assert kept.status_code == 200, kept.text
