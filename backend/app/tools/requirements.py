"""Tools the requirements interviewer uses to keep the requirements list current.

Not part of the chat TOOLSETS: the requirements activity builds these itself.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, tool

from app.requirements.models import CATEGORY_PREFIXES, PRIORITIES, Requirement

logger = logging.getLogger(__name__)

REQUIREMENT_ID = re.compile(r"(FR|NFR|TR)-(\d+)")


@dataclass
class RequirementsContext:
    """In-memory requirements for one turn; the workflow stores the result."""

    requirements: dict[str, Requirement] = field(default_factory=dict)

    @classmethod
    def from_list(cls, requirements: list[Requirement]) -> "RequirementsContext":
        return cls({item["id"]: dict(item) for item in requirements})  # type: ignore[misc]

    def as_list(self) -> list[Requirement]:
        def order(item: Requirement) -> tuple[int, int]:
            match = REQUIREMENT_ID.fullmatch(item["id"])
            prefixes = list(CATEGORY_PREFIXES.values())
            return (prefixes.index(match.group(1)), int(match.group(2))) if match else (9, 0)
        return sorted(self.requirements.values(), key=order)

    def next_id(self, category: str) -> str:
        prefix = CATEGORY_PREFIXES[category]
        numbers = [
            int(match.group(2))
            for match in map(REQUIREMENT_ID.fullmatch, self.requirements)
            if match and match.group(1) == prefix
        ]
        return f"{prefix}-{max(numbers, default=0) + 1}"

    def record(
        self, category: str, title: str, description: str, priority: str,
        rationale: str = "", requirement_id: str | None = None,
    ) -> Requirement:
        category = category.strip().lower().replace("-", "_").replace(" ", "_")
        if category not in CATEGORY_PREFIXES:
            raise ValueError("category must be functional, non_functional, or technical.")
        priority = priority.strip().lower()
        if priority not in PRIORITIES:
            raise ValueError("priority must be must, should, or could.")
        title, description = title.strip(), description.strip()
        if not title or not description:
            raise ValueError("title and description cannot be empty.")
        if requirement_id:
            requirement_id = requirement_id.strip().upper()
            if requirement_id not in self.requirements:
                raise ValueError(f"Unknown requirement id {requirement_id}.")
            if self.requirements[requirement_id]["category"] != category:
                # Moving between categories changes the prefix, so issue a new id.
                del self.requirements[requirement_id]
                requirement_id = self.next_id(category)
        else:
            requirement_id = self.next_id(category)
        requirement: Requirement = {
            "id": requirement_id, "category": category, "title": title,  # type: ignore[typeddict-item]
            "description": description, "priority": priority,  # type: ignore[typeddict-item]
        }
        if rationale.strip():
            requirement["rationale"] = rationale.strip()
        self.requirements[requirement_id] = requirement
        return requirement

    def remove(self, requirement_id: str) -> None:
        requirement_id = requirement_id.strip().upper()
        if self.requirements.pop(requirement_id, None) is None:
            raise ValueError(f"Unknown requirement id {requirement_id}.")


def build(ctx: RequirementsContext) -> list[BaseTool]:
    @tool
    async def record_requirement(
        category: str, title: str, description: str, priority: str,
        rationale: str = "", id: str | None = None,
    ) -> str:
        """Add a requirement, or update one by passing its existing id.

        category: functional, non_functional, or technical.
        priority: must (the user said it is mandatory or required), should
            (important but negotiable), or could (nice to have).
        description: one testable statement of what is required.
        """
        try:
            requirement = ctx.record(category, title, description, priority, rationale, id)
        except ValueError as exc:
            logger.warning("Requirement not recorded: %s", exc)
            return f"Requirement not recorded: {exc}"
        return json.dumps({"action": "recorded", **requirement}, ensure_ascii=False)

    @tool
    async def remove_requirement(id: str) -> str:
        """Remove a requirement the user rejected or that was recorded by mistake."""
        try:
            ctx.remove(id)
        except ValueError as exc:
            logger.warning("Requirement not removed: %s", exc)
            return f"Requirement not removed: {exc}"
        return json.dumps({"action": "removed", "id": id.strip().upper()})

    @tool
    async def list_requirements() -> str:
        """List all requirements recorded so far."""
        return json.dumps(ctx.as_list(), ensure_ascii=False)

    return [record_requirement, remove_requirement, list_requirements]
