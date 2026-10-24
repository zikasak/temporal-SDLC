"""Filters of GET /workflows (FR-66, FR-71 to FR-74, NFR-7), as far as the service answers
without Temporal: malformed filter values are rejected with 422 and a detail, like an
invalid type; well-formed ones are not rejected."""
import uuid

import pytest


def assert_detail(response) -> None:
    assert response.json()["detail"], response.text


@pytest.mark.parametrize("repository", (
    "justaname",
    "owner/",
    "/repo",
    "https://github.com/owner/repo",
), ids=("no-slash", "no-name", "no-owner", "url"))
def test_repository_filter_must_be_owner_slash_name(admin, repository):
    response = admin.get("/workflows", params={"repository": repository})
    assert response.status_code == 422, response.text
    assert_detail(response)


def test_empty_branch_filter_is_rejected(admin):
    response = admin.get("/workflows", params={"branch": ""})
    assert response.status_code == 422, response.text
    assert_detail(response)


@pytest.mark.parametrize("onboarding_id", (
    "abc",
    "onboarding-session-xyz",
    "onboarding-session-" + "a" * 31,
    "onboarding-session-" + "a" * 33,
    "requirements-session-" + "a" * 32,
), ids=("garbage", "not-hex", "too-short", "too-long", "other-prefix"))
def test_onboarding_id_filter_must_be_an_onboarding_session_id(admin, onboarding_id):
    response = admin.get("/workflows", params={"onboarding_id": onboarding_id})
    assert response.status_code == 422, response.text
    assert_detail(response)


def test_malformed_filter_is_rejected_alongside_valid_ones(admin):
    response = admin.get("/workflows", params={
        "type": "OnboardingWorkflow", "branch": "main", "repository": "not-a-repository"})
    assert response.status_code == 422, response.text
    assert_detail(response)


def test_well_formed_filters_are_accepted_and_match_nothing_unknown(admin):
    unknown = uuid.uuid4().hex
    response = admin.get("/workflows", params={
        "type": "OnboardingWorkflow",
        "repository": f"Nobody-{unknown[:8]}/Nothing-{unknown[8:16]}",
        "branch": f"no-such-branch-{unknown}",
        "onboarding_id": f"onboarding-session-{unknown}"})
    # 200 with Temporal, 503 without it; the filter values themselves are valid.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 200:
        assert body["workflows"] == []
    else:
        assert isinstance(body["detail"], str) and body["detail"]


@pytest.mark.parametrize("name,value", (
    ("repository", "owner/repo"),
    ("branch", "main"),
    ("onboarding_id", "onboarding-session-" + "0" * 32),
))
def test_each_single_filter_is_accepted(admin, name, value):
    response = admin.get("/workflows", params={name: value})
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 200:
        assert isinstance(body["workflows"], list)
    else:
        assert isinstance(body["detail"], str) and body["detail"]


def test_listed_executions_always_carry_repository_branch_and_onboarding_id(admin):
    response = admin.get("/workflows")
    assert response.status_code in (200, 503), response.text
    body = response.json()
    if response.status_code == 503:
        assert isinstance(body["detail"], str) and body["detail"]
        return
    for item in body["workflows"]:
        for field in ("workflow_id", "run_id", "workflow_type", "status", "task_queue",
                      "start_time"):
            assert item[field]
        for field in ("repository", "branch", "onboarding_id"):
            assert field in item, field
        if item["repository"] is None:
            assert item["branch"] is None
        else:
            assert item["branch"] == item["repository"]["branch"]
        if item["workflow_type"] == "OnboardingWorkflow":
            assert item["onboarding_id"] == item["workflow_id"]
