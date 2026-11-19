# Assertions read optional fields directly: a None there fails the test anyway.
# pyright: reportOptionalMemberAccess=false
from typing import cast

import pytest

from app.requirements.document import (
    render_requirements_markdown, requirements_narrative, without_repeated_tables,
)
from app.requirements.models import Category, Requirement
from app.tools.requirements import RequirementsContext, build


def test_record_assigns_ids_per_category():
    ctx = RequirementsContext()
    first = ctx.record("functional", "Book rooms", "Users can book a room.", "must")
    second = ctx.record("Non-functional", "Fast", "Pages load within 1 s.", "Should")
    third = ctx.record("functional", "Cancel", "Users can cancel a booking.", "could")
    assert [first["id"], second["id"], third["id"]] == ["FR-1", "NFR-1", "FR-2"]
    assert second["category"] == "non_functional"
    assert [item["id"] for item in ctx.as_list()] == ["FR-1", "FR-2", "NFR-1"]


def test_record_updates_and_moves_between_categories():
    ctx = RequirementsContext()
    ctx.record("functional", "SSO", "Users sign in with SSO.", "must")
    updated = ctx.record("functional", "SSO", "Users sign in with Okta SSO.", "must",
                         requirement_id="fr-1")
    assert updated["id"] == "FR-1" and "Okta" in updated["description"]
    moved = ctx.record("technical", "SSO", "Use Okta via OIDC.", "must", requirement_id="FR-1")
    assert moved["id"] == "TR-1"
    assert [item["id"] for item in ctx.as_list()] == ["TR-1"]


@pytest.mark.parametrize("kwargs", [
    {"category": "legal"},
    {"priority": "urgent"},
    {"title": " "},
    {"requirement_id": "FR-9"},
])
def test_record_rejects_invalid_input(kwargs):
    arguments = {"category": "functional", "title": "T", "description": "D",
                 "priority": "must", **kwargs}
    with pytest.raises(ValueError):
        RequirementsContext().record(**arguments)


def test_remove():
    ctx = RequirementsContext()
    ctx.record("technical", "Postgres", "Store data in PostgreSQL.", "must")
    ctx.remove("tr-1")
    assert ctx.as_list() == []
    with pytest.raises(ValueError):
        ctx.remove("TR-1")


async def test_set_task_kind_records_only_known_kinds():
    ctx = RequirementsContext()
    tools = {item.name: item for item in build(ctx)}
    refused = await tools["set_task_kind"].ainvoke({"kind": "refactoring"})
    assert "not recorded" in refused and "bugfix" in refused
    assert ctx.kind is None
    await tools["set_task_kind"].ainvoke({"kind": " API change "})
    assert ctx.kind == "api_change"


@pytest.mark.parametrize("kind", ["bugfix", "api_change"])
async def test_change_to_a_published_service_continues_its_requirement_ids(kind):
    categories: list[tuple[str, Category]] = [
        ("FR-1", "functional"), ("FR-2", "functional"), ("NFR-1", "non_functional"),
        ("TR-5", "technical")]
    ctx = RequirementsContext(published=tuple(
        Requirement(id=item, category=category, title="T", description="D", priority="must")
        for item, category in categories))
    # Until the user says the task changes that service, it is numbered on its own.
    assert ctx.record("functional", "Sign in", "D", "must")["id"] == "FR-1"
    assert ctx.record("technical", "Postgres", "D", "must")["id"] == "TR-1"
    set_task_kind = build(ctx)[0]
    # The analyst is told what changed, so the conversation never keeps a stale ID silently.
    assert '"renumbered": {"FR-1": "FR-3"}' in await set_task_kind.ainvoke({"kind": kind})
    assert [item["id"] for item in ctx.as_list()] == ["FR-3", "TR-1"]
    # In the usual order, kind first, an ID is right from the moment it is given.
    assert ctx.record("functional", "Sign out", "D", "must")["id"] == "FR-4"
    assert ctx.record("non_functional", "Fast", "D", "should")["id"] == "NFR-2"
    assert "renumbered" not in await set_task_kind.ainvoke({"kind": kind})
    # A new service does not join the repository's contract.
    await set_task_kind.ainvoke({"kind": "new_service"})
    assert ctx.record("technical", "Redis", "D", "must")["id"] == "TR-2"


async def test_only_a_change_retires_requirements_and_only_the_services():
    service: tuple[Requirement, ...] = ({"id": "FR-1", "category": "functional", "title": "Book rooms",
                "description": "Guests book rooms.", "priority": "must"},)
    ctx = RequirementsContext(published=service)
    tools = {item.name: item for item in build(ctx)}
    # Until the user has said the task changes that service, nothing of it can end.
    refused = await tools["retire_requirement"].ainvoke({"id": "FR-1"})
    assert refused.startswith("Requirement not retired:") and ctx.retired_list() == []
    await tools["set_task_kind"].ainvoke({"kind": "api_change"})
    # The change's own requirement, FR-2, is removed, not retired.
    assert ctx.record("functional", "Cancel", "D", "must")["id"] == "FR-2"
    assert "not a requirement of the existing service" in await tools[
        "retire_requirement"].ainvoke({"id": "FR-2"})
    assert '"action": "retired"' in await tools["retire_requirement"].ainvoke({"id": " fr-1 "})
    assert ctx.retired_list() == list(service)
    await tools["keep_requirement"].ainvoke({"id": "FR-1"})
    assert ctx.retired_list() == []
    # A new service has no earlier requirements to end.
    await tools["retire_requirement"].ainvoke({"id": "FR-1"})
    await tools["set_task_kind"].ainvoke({"kind": "new_service"})
    assert ctx.retired_list() == [] and not ctx.retired


def test_document_names_retired_requirements_and_keeps_its_narrative_readable():
    kept: list[Requirement] = [{"id": "FR-2", "category": "functional", "title": "Cancel",
             "description": "Guests cancel.", "priority": "must"}]
    retired: list[Requirement] = [{"id": "FR-1", "category": "functional", "title": "No refunds",
                "description": "A booking | is final.", "priority": "must"}]
    document = render_requirements_markdown(kept, "## Overview\n\nRefunds.", retired)
    assert "| FR-1 | No refunds | A booking \\| is final. |" in document
    # The coding agents still get the tables once: what is retired travels as narrative.
    narrative = requirements_narrative(document, kept)
    assert narrative.startswith("## Overview") and "## Retired requirements" in (narrative or '')


def test_render_contains_every_requirement_and_escapes_tables():
    ctx = RequirementsContext()
    ctx.record("functional", "Search", "Search by name | date.", "must", "Users asked")
    ctx.record("technical", "Python", "Backend in Python.", "should")
    markdown = render_requirements_markdown(ctx.as_list(), "## Overview\n\nRoom booking.")
    assert markdown.startswith("# Requirements specification\n\n## Overview")
    assert "| FR-1 | Search | Search by name \\| date. | Must | Users asked |" in markdown
    assert "| TR-1 | Python | Backend in Python. | Should |  |" in markdown
    non_functional = markdown.split("## Non-functional requirements")[1]
    assert non_functional.split("##")[0].strip() == "_None identified._"


@pytest.mark.parametrize("narrative", ["Bookings are never deleted.\n\n## Assumptions\nOne site.", None])
def test_coding_context_carries_requirements_once(narrative):
    requirements: list[Requirement] = [
        {"id": "FR-1", "category": "functional", "title": "Book | rooms",
         "description": "Users can\nbook a room.", "priority": "must", "rationale": "Core"},
        {"id": "NFR-1", "category": "non_functional", "title": "Fast",
         "description": "Pages load within 1 s.", "priority": "should"},
    ]
    context = {"requirements": requirements, "api_design": {"endpoints": []},
               "requirements_document": render_requirements_markdown(requirements, narrative)}
    expected = {"requirements": requirements, "api_design": {"endpoints": []}}
    if narrative:
        expected["requirements_narrative"] = narrative
    assert without_repeated_tables(context) == expected


@pytest.mark.parametrize("change", ["edited", "stale", "foreign", "malformed"])
def test_coding_context_keeps_documents_it_cannot_account_for(change):
    requirements: list[Requirement] = [{"id": "FR-1", "category": "functional",
                                        "title": "Book rooms",
                                        "description": "Users can book a room.",
                                        "priority": "must"}]
    document = render_requirements_markdown(requirements, "Overview")
    if change == "edited":
        document = document.replace("Users can book a room.", "Members can book a room.")
    elif change == "stale":
        requirements = [{**requirements[0], "priority": "could"}]
    elif change == "foreign":
        document = "Uploaded specification\n" + document
    else:
        # Deliberately not a requirement.
        requirements = cast(list[Requirement], [{"id": "FR-1"}])
    context = {"requirements": requirements, "requirements_document": document}
    assert without_repeated_tables(context) == context
