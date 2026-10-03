"""Renders the requirements document."""

from app.requirements.models import Requirement

DOCUMENT_FILENAME = "requirements.md"
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
    parts = ["# Requirements specification"]
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
