"""Renders the requirements document."""

from typing import Any

from app.requirements.models import Requirement

DOCUMENT_FILENAME = "requirements.md"
TITLE = "# Requirements specification"
SECTIONS = (
    ("functional", "Functional requirements"),
    ("non_functional", "Non-functional requirements"),
    ("technical", "Technical requirements"),
)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


RETIRED_HEADING = "## Retired requirements"
CONTRADICTIONS_HEADING = "## Unresolved contradictions"


def render_requirements_markdown(
    requirements: list[Requirement], narrative: str | None = None,
    retired: list[Requirement] | None = None,
    contradictions: list[dict[str, Any]] | None = None,
) -> str:
    """The requirement sections come straight from the list, so none are lost. `retired`
    are the requirements of the existing service that a change to it ends;
    `contradictions` those the requirements review still reported when the user
    continued despite them."""
    parts = [TITLE]
    if narrative and narrative.strip():
        parts.append(narrative.strip())
    if retired:
        # Ahead of the tables, so the document still ends with exactly those.
        parts.append(RETIRED_HEADING)
        parts.append("This change ends these requirements of the existing service. They "
                     "no longer hold once it is made.")
        parts.append("\n".join(["| ID | Requirement | Description |", "| --- | --- | --- |"] + [
            f"| {item['id']} | {_cell(item['title'])} | {_cell(item['description'])} |"
            for item in retired]))
    if contradictions:
        # Also ahead of the tables.
        parts.append(CONTRADICTIONS_HEADING)
        parts.append("The requirements review still reported these contradictions when the "
                     "user chose to continue despite them.")
        parts.append("\n".join(["| ID | Requirements | Contradiction |", "| --- | --- | --- |"] + [
            f"| {_cell(str(item.get('id', '')))} "
            f"| {_cell(', '.join(str(ref) for ref in item.get('requirement_ids') or []))} "
            f"| {_cell(str(item.get('summary', '')))} |"
            for item in contradictions]))
    for category, heading in SECTIONS:
        items = [item for item in requirements if item["category"] == category]
        parts.append(f"## {heading}")
        if not items:
            parts.append("_None identified._")
            continue
        rows = ["| ID | Requirement | Description | Priority | Rationale |",
                "| --- | --- | --- | --- | --- |"]
        rows += [
            f"| {item['id']} | {_cell(item['title'])} | {_cell(item['description'])} "
            f"| {item['priority'].capitalize()} | {_cell(item.get('rationale', ''))} |"
            for item in items
        ]
        parts.append("\n".join(rows))
    return "\n\n".join(parts) + "\n"


def requirements_narrative(document: str, requirements: list[Requirement]) -> str | None:
    """Text the document adds to the list; None unless its tables exactly render that list."""
    try:
        tables = render_requirements_markdown(requirements)[len(TITLE):]
    except (KeyError, TypeError, AttributeError):
        return None
    if not document.startswith(TITLE) or not document.endswith(tables):
        return None
    return document[len(TITLE):-len(tables)].strip()


def without_repeated_tables(context: dict[str, Any]) -> dict[str, Any]:
    """Coding context whose document is reduced to its narrative when that loses nothing.

    An edited, foreign or otherwise unrecognised document is passed through whole.
    The brief of a change to an existing service is reduced the same way.
    """
    result = dict(context)
    if result.get("brief"):
        result["brief"] = without_repeated_tables(result["brief"])
    narrative = requirements_narrative(context.get("requirements_document") or "",
                                       context.get("requirements") or [])
    if narrative is None:
        return result
    del result["requirements_document"]
    if narrative:
        result["requirements_narrative"] = narrative
    return result
