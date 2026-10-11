import pytest

from app.requirements.document import render_requirements_markdown, without_repeated_tables
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
    requirements = [
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
    requirements = [{"id": "FR-1", "category": "functional", "title": "Book rooms",
                     "description": "Users can book a room.", "priority": "must"}]
    document = render_requirements_markdown(requirements, "Overview")
    if change == "edited":
        document = document.replace("Users can book a room.", "Members can book a room.")
    elif change == "stale":
        requirements = [{**requirements[0], "priority": "could"}]
    elif change == "foreign":
        document = "Uploaded specification\n" + document
    else:
        requirements = [{"id": "FR-1"}]
    context = {"requirements": requirements, "requirements_document": document}
    assert without_repeated_tables(context) == context
