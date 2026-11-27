import uuid
from datetime import datetime

from conftest import PASSWORD, new_client


def new_login() -> str:
    return f"user-{uuid.uuid4().hex[:12]}"


def test_first_account_is_the_administrator(admin_session):
    assert admin_session["token_type"] == "bearer"
    assert admin_session["access_token"]
    assert admin_session["admin"] is True
    datetime.fromisoformat(admin_session["expires_at"])


def test_registration_closes_once_an_account_exists(admin_session, anonymous):
    assert anonymous.get("/auth/registration").json() == {"open": False}
    response = anonymous.post("/auth/register", json={"login": new_login(), "password": PASSWORD})
    assert response.status_code == 403


def test_profile_of_the_signed_in_user(admin_session, admin):
    response = admin.get("/auth/me")
    assert response.status_code == 200
    assert response.json() == {"login": admin_session["login"],
                               "expires_at": admin_session["expires_at"], "admin": True}


def test_endpoints_require_sign_in(anonymous):
    for path in ("/auth/me", "/auth/users", "/connectors/github/connection"):
        response = anonymous.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer"
    forged = anonymous.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert forged.status_code == 401


def test_wrong_password_is_rejected(admin_session, anonymous):
    response = anonymous.post("/auth/login", json={"login": admin_session["login"],
                                                  "password": f"{PASSWORD}-wrong"})
    assert response.status_code == 401
    assert "access_token" not in response.cookies


def test_administrator_creates_an_account_that_can_sign_in(admin, anonymous, base_url):
    login = new_login()
    created = admin.post("/auth/users", json={"login": f"  {login.upper()} ", "password": PASSWORD})
    assert created.status_code == 201
    assert created.json()["login"] == login
    datetime.fromisoformat(created.json()["created_at"])
    assert login in [item["login"] for item in admin.get("/auth/users").json()]

    duplicate = admin.post("/auth/users", json={"login": login, "password": PASSWORD})
    assert duplicate.status_code == 409

    session = anonymous.post("/auth/login", json={"login": login, "password": PASSWORD})
    assert session.status_code == 200
    assert session.json()["admin"] is False
    # The session cookie alone signs the user in, and only the administrator manages
    # accounts.
    with new_client(base_url) as client:
        client.cookies.set("access_token", session.cookies["access_token"])
        assert client.get("/auth/me").json()["login"] == login
        assert client.get("/auth/users").status_code == 403
        assert client.post("/auth/users", json={"login": new_login(),
                                                "password": PASSWORD}).status_code == 403


def test_account_details_are_validated(admin):
    for body in ({"login": "ab", "password": PASSWORD},
                 {"login": "has space", "password": PASSWORD},
                 {"login": new_login(), "password": "short"}):
        response = admin.post("/auth/users", json=body)
        assert response.status_code == 422, body
        assert body["password"] not in response.text


def test_logout_clears_the_session_cookie(anonymous):
    response = anonymous.post("/auth/logout")
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("access_token=") and "Max-Age=0" in cookie
