"""Model settings (FR-148): the requirements reviewer's model is configured next to the other
LLM workflow types. Without an LLM provider the service may be unable to list models, so the
tests accept 503 where the contract documents it and check input validation, which does not
depend on a provider."""
import pytest

WORKFLOW_TYPES = {"chat", "requirements", "requirements_review", "api_design", "implementation",
                  "test_generation", "diagnosis", "documentation", "review", "routing"}
ADAPTERS = {"pi", "openai", "claude"}


def model_settings() -> dict:
    return {"adapter": "claude", "model": "some-model", "thinking_level": "high"}


def full_body() -> dict:
    return {"workflows": {name: model_settings() for name in sorted(WORKFLOW_TYPES)}}


def body_without(name: str) -> dict:
    body = full_body()
    del body["workflows"][name]
    return body


def test_settings_require_sign_in(anonymous):
    for method, body in (("GET", None), ("PUT", full_body())):
        response = anonymous.request(method, "/settings", json=body)
        assert response.status_code == 401, method
        assert response.headers["www-authenticate"] == "Bearer"


def test_settings_view_lists_the_requirements_review_next_to_every_other_type(admin):
    response = admin.get("/settings")
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    assert set(body["workflows"]) == WORKFLOW_TYPES
    for name, settings in body["workflows"].items():
        assert settings["adapter"] in ADAPTERS, name
        assert isinstance(settings["model"], str) and settings["model"], name
        assert isinstance(settings["thinking_level"], str) and settings["thinking_level"], name
        assert isinstance(settings["compaction_tokens"], int), name
        assert settings["compaction_tokens"] == 0 or settings["compaction_tokens"] >= 4000, name
    assert isinstance(body["agent"]["contract_embed_chars"], int)
    assert 1000 <= body["agent"]["contract_embed_chars"] <= 10_000_000
    assert set(body["workflow_labels"]) >= WORKFLOW_TYPES
    assert body["workflow_labels"]["requirements_review"] == "Requirements review"
    assert all(label for label in body["workflow_labels"].values())
    assert isinstance(body["providers"], dict)
    assert set(body["providers"]) <= ADAPTERS


@pytest.mark.parametrize("missing", sorted(WORKFLOW_TYPES))
def test_saving_settings_needs_every_workflow_type(admin, missing):
    # requirements_review is the new type: a client sending the former set gets 422.
    response = admin.put("/settings", json=body_without(missing))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_saving_settings_rejects_an_unknown_workflow_type(admin):
    body = full_body()
    body["workflows"]["no_such_workflow"] = model_settings()
    response = admin.put("/settings", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("change", (
    {"adapter": "gemini"},
    {"adapter": None},
    {"model": ""},
    {"model": "m" * 301},
    {"model": "bad\x00model"},
    {"thinking_level": ""},
    {"thinking_level": "t" * 101},
    {"thinking_level": None},
    {"compaction_tokens": -1},
    {"compaction_tokens": 3999},
    {"compaction_tokens": "many"},
), ids=("unknown-adapter", "null-adapter", "empty-model", "model-301", "control-character",
        "empty-thinking-level", "thinking-level-101", "null-thinking-level",
        "negative-compaction", "compaction-below-4000", "compaction-not-a-number"))
def test_saving_settings_validates_the_requirements_review_entry(admin, change):
    body = full_body()
    body["workflows"]["requirements_review"] = {**model_settings(), **change}
    response = admin.put("/settings", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("body", ({}, {"workflows": None}, {"workflows": []}, {"workflows": "all"}),
                         ids=("no-workflows", "null", "list", "string"))
def test_saving_settings_needs_a_workflows_object(admin, body):
    response = admin.put("/settings", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


@pytest.mark.parametrize("agent", (
    {"contract_embed_chars": 999},
    {"contract_embed_chars": 10_000_001},
    {"contract_embed_chars": None},
    {"unknown": 1},
), ids=("embed-999", "embed-over-10000000", "null-embed", "unknown-field"))
def test_saving_settings_validates_the_agent_settings(admin, agent):
    response = admin.put("/settings", json={**full_body(), "agent": agent})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_rejected_settings_are_not_stored(admin):
    before = admin.get("/settings")
    rejected = admin.put("/settings", json=body_without("requirements_review"))
    assert rejected.status_code == 422, rejected.text
    after = admin.get("/settings")
    assert after.status_code == before.status_code
    if before.status_code == 200:
        assert after.json()["workflows"] == before.json()["workflows"]
        assert after.json()["agent"] == before.json()["agent"]
