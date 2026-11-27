"""Tools the requirements interviewer uses to keep the requirements list current.

Not part of the chat TOOLSETS: the requirements activity builds these itself.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, tool

from app.requirements.models import (
    CATEGORY_PREFIXES, CHANGE_KINDS, PRIORITIES, TASK_KINDS, Requirement, TaskKind,
    numbered_after,
)

logger = logging.getLogger(__name__)

REQUIREMENT_ID = re.compile(r"(FR|NFR|TR)-(\d+)")


@dataclass
class RequirementsContext:
    """In-memory requirements for one turn; the workflow stores the result."""

    requirements: dict[str, Requirement] = field(default_factory=dict)
    kind: TaskKind | None = None
    # The requirements in the repository's contract, when it has one.
    published: tuple[Requirement, ...] = ()
    # IDs of those the change ends.
    retired: set[str] = field(default_factory=set)

    @classmethod
    def from_list(cls, requirements: list[Requirement], kind: TaskKind | None = None,
                  published: tuple[Requirement, ...] = (),
                  retired: list[Requirement] | None = None) -> "RequirementsContext":
        return cls({item["id"]: dict(item) for item in requirements}, kind,  # type: ignore[misc]
                   published, {item["id"] for item in retired or []})

    def service_requirements(self) -> tuple[Requirement, ...]:
        """The contract a task that changes the repository's service starts from."""
        return self.published if self.kind in CHANGE_KINDS else ()

    def _reserved(self) -> tuple[str, ...]:
        """A task that changes the repository's service adds its requirements to that
        contract, so its IDs continue the contract's instead of repeating them."""
        return tuple(item["id"] for item in self.service_requirements())

    def retired_list(self) -> list[Requirement]:
        return [item for item in self.service_requirements() if item["id"] in self.retired]

    def retire(self, requirement_id: str, retire: bool = True) -> Requirement:
        """End a requirement of the existing service, or with `retire` false keep it
        after all. Only a requirement of the repository's contract can be named."""
        requirement_id = requirement_id.strip().upper()
        if self.kind not in CHANGE_KINDS:
            raise ValueError("only a change to an existing service can retire its requirements.")
        found = next((item for item in self.published if item["id"] == requirement_id), None)
        if found is None:
            raise ValueError(f"{requirement_id} is not a requirement of the existing service.")
        (self.retired.add if retire else self.retired.discard)(requirement_id)
        return found

    def set_kind(self, kind: str) -> dict[str, str]:
        """Only a known kind can be recorded; anything else raises. Returns the new ID
        of each requirement, recorded under another kind, that the new kind renumbers."""
        kind = kind.strip().lower().replace("-", "_").replace(" ", "_")
        if kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(TASK_KINDS)}.")
        self.kind = kind  # type: ignore[assignment]
        if kind not in CHANGE_KINDS:
            self.retired.clear()
        before = self.as_list()
        after = numbered_after(before, self._reserved())
        self.requirements = {item["id"]: item for item in after}
        return {old["id"]: new["id"] for old, new in zip(before, after) if old["id"] != new["id"]}

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
            for match in map(REQUIREMENT_ID.fullmatch, [*self.requirements, *self._reserved()])
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
    async def set_task_kind(kind: str) -> str:
        """Record what kind of task this is, once the user has confirmed the kind you
        proposed or named another one themselves.

        kind: new_service, api_change, bugfix (also any other change that leaves
            the API as it is), or docs. Never call this with an unconfirmed guess.
        """
        try:
            renumbered = ctx.set_kind(kind)
        except ValueError as exc:
            logger.warning("Task kind not recorded: %s", exc)
            return f"Task kind not recorded: {exc}"
        result: dict[str, object] = {"action": "recorded", "kind": ctx.kind}
        if renumbered:
            result["renumbered"] = renumbered
            result["note"] = ("These requirements have new IDs, because the service already "
                              "has requirements with their old ones. Tell the user each old "
                              "and new ID, and use only the new IDs from now on.")
        return json.dumps(result)

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

    @tool
    async def retire_requirement(id: str) -> str:
        """End a requirement of the existing service that this change contradicts,
        replaces or removes. It leaves the service's contract once the change is
        delivered. Record what holds instead, if anything, with record_requirement.

        id: the ID of a requirement of the existing service, not of this change.
        """
        try:
            requirement = ctx.retire(id)
        except ValueError as exc:
            logger.warning("Requirement not retired: %s", exc)
            return f"Requirement not retired: {exc}"
        return json.dumps({"action": "retired", **requirement}, ensure_ascii=False)

    @tool
    async def keep_requirement(id: str) -> str:
        """Undo retire_requirement: the existing service's requirement holds after all."""
        try:
            requirement = ctx.retire(id, retire=False)
        except ValueError as exc:
            logger.warning("Requirement not kept: %s", exc)
            return f"Requirement not kept: {exc}"
        return json.dumps({"action": "kept", **requirement}, ensure_ascii=False)

    return [set_task_kind, record_requirement, remove_requirement, list_requirements,
            retire_requirement, keep_requirement]
