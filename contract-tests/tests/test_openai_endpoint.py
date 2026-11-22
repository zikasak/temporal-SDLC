"""The OpenAI endpoint section of GET/PUT /settings (FR-174 to FR-182, NFR-8, TR-7).

The suite has no LLM provider and no OpenAI-compatible server it could point the service at, so
a successful save of an endpoint (which makes the service send GET {base_url}/models) is not
exercised. What is checked: the shape of the section by role, that the key never appears,
administrator-only access, input validation, the "changed base URL needs a new key" rule, the
failed endpoint check, and that rejected requests leave the stored state untouched.

Requests that must succeed re-send the workflow settings the service itself returned, so the
suite needs no knowledge of the models a provider offers. Such a save may legitimately answer
503 when a provider's models cannot be listed (documented), which is accepted next to 200.
"""
import uuid

import pytest

from conftest import PASSWORD, new_client

SECRET_KEY = "sk-contract-test-" + uuid.uuid4().hex


@pytest.fixture(scope="module")
def member_client(base_url, admin_session):
    """A signed-in account that is not the administrator."""
    login = f"member-{uuid.uuid4().hex[:12]}"
    with new_client(base_url, admin_session["access_token"]) as admin_client:
        created = admin_client.post("/auth/users", json={"login": login, "password": PASSWORD})
        assert created.status_code == 201, created.text
    with new_client(base_url) as anonymous:
        session = anonymous.post("/auth/login", json={"login": login, "password": PASSWORD})
    assert session.status_code == 200, session.text
    assert session.json()["admin"] is False
    with new_client(base_url, session.json()["access_token"]) as client:
        yield client


def get_settings(client) -> dict:
    response = client.get("/settings")
    assert response.status_code == 200, response.text
    return response.json()


def current_workflows(client) -> dict:
    return get_settings(client)["workflows"]


def unreachable_url() -> str:
    # The .invalid TLD never resolves, so a check against it cannot succeed.
    return f"https://endpoint-{uuid.uuid4().hex[:12]}.invalid/v1"


def keys_of(value) -> set:
    """Every object key anywhere in a JSON value."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_of(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_of(item)
    return found


def assert_detail(response):
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


# View of the section, by role (FR-178, FR-176, FR-177, NFR-8)

def test_administrator_sees_base_url_display_name_and_key_flag(admin):
    view = get_settings(admin)
    section = view["openai_endpoint"]
    assert set(section) == {"base_url", "display_name", "api_key_set"}
    assert section["base_url"] is None or isinstance(section["base_url"], str)
    assert section["display_name"] is None or isinstance(section["display_name"], str)
    assert isinstance(section["api_key_set"], bool)
    # No API key value or field is ever part of the response.
    assert "api_key" not in keys_of(view)


def test_other_users_see_only_the_display_name(member_client):
    view = get_settings(member_client)
    section = view["openai_endpoint"]
    assert "base_url" not in section
    assert "api_key_set" not in section
    assert section.get("display_name") is None or isinstance(section["display_name"], str)
    assert "api_key" not in keys_of(view)


def test_openai_adapter_is_offered_exactly_while_a_base_url_is_saved(admin, member_client):
    admin_view = get_settings(admin)
    saved = admin_view["openai_endpoint"]["base_url"] is not None
    assert ("openai" in admin_view["providers"]) is saved
    # The other users' view follows the same rule, though it does not show the URL.
    assert ("openai" in get_settings(member_client)["providers"]) is saved


def test_workflow_types_without_an_endpoint_do_not_default_to_openai(admin):
    view = get_settings(admin)
    assert view["openai_endpoint"]["base_url"] is None, \
        "this test needs a service with no OpenAI endpoint saved"
    # FR-179: while OpenAI is not set up a workflow type with no saved adapter uses pi.
    assert all(settings["adapter"] != "openai" for settings in view["workflows"].values())


def test_settings_view_is_the_same_for_every_user_apart_from_the_endpoint_section(admin,
                                                                                 member_client):
    admin_view = get_settings(admin)
    member_view = get_settings(member_client)
    assert set(member_view["workflows"]) == set(admin_view["workflows"])
    assert member_view["workflow_labels"] == admin_view["workflow_labels"]
    assert (member_view["openai_endpoint"].get("display_name")
            == admin_view["openai_endpoint"]["display_name"])


# Administrator only (FR-175, FR-182)

@pytest.mark.parametrize("section", (
    None,
    {"base_url": "https://llm.example.com/v1", "api_key": SECRET_KEY, "display_name": "Mine"},
), ids=("removal", "save"))
def test_other_users_cannot_send_the_endpoint_section(admin, member_client, section):
    before = get_settings(admin)["openai_endpoint"]
    body = {"workflows": current_workflows(member_client), "openai_endpoint": section}
    response = member_client.put("/settings", json=body)
    assert response.status_code == 403, response.text
    assert_detail(response)
    assert SECRET_KEY not in response.text
    assert get_settings(admin)["openai_endpoint"] == before


def test_other_users_save_their_workflow_settings_without_the_section(admin, member_client):
    before = get_settings(admin)["openai_endpoint"]
    response = member_client.put("/settings", json={"workflows": current_workflows(member_client)})
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        assert_detail(response)
    else:
        saved = response.json()
        assert set(saved["workflows"]) == set(current_workflows(admin))
        assert "base_url" not in saved["openai_endpoint"]
        assert "api_key_set" not in saved["openai_endpoint"]
    assert get_settings(admin)["openai_endpoint"] == before


# Removal (FR-182)

def test_removing_an_endpoint_that_is_not_saved_succeeds_without_change(admin):
    view = get_settings(admin)
    # With an endpoint saved earlier removal would be a real change, which workflows using
    # openai may block (another rule), so this test needs a service without one.
    assert view["openai_endpoint"]["base_url"] is None, \
        "this test needs a service with no OpenAI endpoint saved"
    response = admin.put("/settings", json={"workflows": view["workflows"],
                                            "openai_endpoint": None})
    assert response.status_code in (200, 503), response.text
    if response.status_code == 503:
        assert_detail(response)
        return
    saved = response.json()
    assert saved["openai_endpoint"]["base_url"] is None
    assert saved["openai_endpoint"]["display_name"] is None
    assert saved["openai_endpoint"]["api_key_set"] is False
    assert "openai" not in saved["providers"]
    assert get_settings(admin)["openai_endpoint"] == saved["openai_endpoint"]


# Input validation (FR-180, NFR-8)

@pytest.mark.parametrize("endpoint", (
    {},
    {"api_key": SECRET_KEY},
    {"base_url": None},
    {"base_url": ""},
    {"base_url": "   "},
    {"base_url": "llm.example.com/v1"},
    {"base_url": "ftp://llm.example.com/v1"},
    {"base_url": "file:///etc/passwd"},
    {"base_url": "javascript:alert(1)"},
    {"base_url": 12345},
    {"base_url": "https://" + "a" * 2050 + ".example.com/v1"},
    {"base_url": "https://llm.example.com/v1", "display_name": "d" * 101},
    {"base_url": "https://llm.example.com/v1", "display_name": 42},
    {"base_url": "https://llm.example.com/v1", "api_key": 12345},
), ids=("empty-object", "no-base-url", "null-base-url", "empty-base-url", "blank-base-url",
        "no-scheme", "ftp", "file", "javascript", "number", "over-2048", "name-101",
        "name-number", "key-number"))
def test_invalid_endpoint_is_rejected_and_nothing_is_saved(admin, endpoint):
    before = get_settings(admin)
    response = admin.put("/settings", json={"workflows": before["workflows"],
                                            "openai_endpoint": endpoint})
    assert response.status_code == 422, response.text
    assert_detail(response)
    assert SECRET_KEY not in response.text
    after = get_settings(admin)
    assert after["openai_endpoint"] == before["openai_endpoint"]
    assert after["workflows"] == before["workflows"]


@pytest.mark.parametrize("section", ("https://llm.example.com/v1", ["x"], 7, True),
                         ids=("string", "list", "number", "boolean"))
def test_endpoint_section_must_be_an_object_or_null(admin, section):
    response = admin.put("/settings", json={"workflows": current_workflows(admin),
                                            "openai_endpoint": section})
    assert response.status_code == 422, response.text
    assert_detail(response)


def test_display_name_longer_than_100_characters_after_trimming_is_rejected(admin):
    response = admin.put("/settings", json={
        "workflows": current_workflows(admin),
        "openai_endpoint": {"base_url": "https://llm.example.com/v1", "api_key": SECRET_KEY,
                            "display_name": " " + "n" * 101 + " "}})
    assert response.status_code == 422, response.text
    assert_detail(response)


def test_endpoint_needs_the_workflow_settings_next_to_it(admin):
    response = admin.put("/settings", json={"openai_endpoint": None})
    assert response.status_code == 422, response.text
    assert_detail(response)


# Check on save (FR-180) and the stored key (NFR-8)

def test_a_changed_base_url_without_a_new_key_is_rejected(admin):
    before = get_settings(admin)
    changed = unreachable_url()
    assert before["openai_endpoint"]["base_url"] != changed
    for endpoint in ({"base_url": changed}, {"base_url": changed, "api_key": None}):
        response = admin.put("/settings", json={"workflows": before["workflows"],
                                                "openai_endpoint": endpoint})
        assert response.status_code == 422, response.text
        assert_detail(response)
    after = get_settings(admin)
    assert after["openai_endpoint"] == before["openai_endpoint"]
    assert after["workflows"] == before["workflows"]


def test_an_endpoint_that_fails_the_check_is_not_saved_and_the_key_is_not_echoed(admin):
    before = get_settings(admin)
    url = unreachable_url()
    response = admin.put("/settings", json={
        "workflows": before["workflows"],
        "openai_endpoint": {"base_url": url, "api_key": SECRET_KEY, "display_name": "Check me"}})
    assert response.status_code == 422, response.text
    assert_detail(response)
    assert SECRET_KEY not in response.text
    after = get_settings(admin)
    assert after["openai_endpoint"] == before["openai_endpoint"]
    assert set(after["providers"]) == set(before["providers"])
    assert SECRET_KEY not in str(after)
