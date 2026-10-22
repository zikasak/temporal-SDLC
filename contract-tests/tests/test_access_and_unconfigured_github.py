"""Account administration needs sign-in, and the GitHub endpoints that depend on a
connected app refuse to work while none is connected (FR-6, FR-9, FR-50, NFR-6)."""
import pytest

from conftest import PASSWORD


def test_account_administration_requires_sign_in(anonymous):
    listing = anonymous.get("/auth/users")
    assert listing.status_code == 401
    assert listing.headers["www-authenticate"] == "Bearer"
    created = anonymous.post("/auth/users", json={"login": "anonymous-made",
                                                  "password": PASSWORD})
    assert created.status_code == 401
    assert created.headers["www-authenticate"] == "Bearer"


def test_registration_status_needs_no_sign_in(anonymous):
    response = anonymous.get("/auth/registration")
    assert response.status_code == 200
    assert isinstance(response.json()["open"], bool)


@pytest.mark.parametrize("path,allowed", (
    ("/connectors/github/repositories", (409, 503)),
    ("/connectors/github/repositories/owner/repo/branches", (404, 409, 503)),
), ids=("repositories", "branches"))
def test_browsing_without_a_connected_app_is_refused(admin, path, allowed):
    response = admin.get(path)
    assert response.status_code in allowed, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


def test_checking_without_a_connected_app_is_refused(admin):
    response = admin.post("/connectors/github/connection/check", json={})
    assert response.status_code in (409, 503), response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]
    assert admin.get("/connectors/github/connection").json()["configured"] is False


@pytest.mark.parametrize("method,path", (
    ("POST", "/connectors/github/connection/check"),
    ("DELETE", "/connectors/github/connection"),
), ids=("check", "delete"))
def test_connection_actions_need_a_json_object_body(admin, method, path):
    response = admin.request(method, path, json=[])
    assert response.status_code == 422, response.text
    assert response.json()["detail"]
