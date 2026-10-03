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


def render_requirements_markdown(
    requirements: list[Requirement], narrative: str | None = None
) -> str:
    """The requirement sections come straight from the list, so none are lost."""
    parts = [TITLE]
    if narrative and narrative.strip():
        parts.append(narrative.strip())
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
    """
    narrative = requirements_narrative(context.get("requirements_document") or "",
                                       context.get("requirements") or [])
    if narrative is None:
        return dict(context)
    result = {key: value for key, value in context.items() if key != "requirements_document"}
    if narrative:
        result["requirements_narrative"] = narrative
    return result
