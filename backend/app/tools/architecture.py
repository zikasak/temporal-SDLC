"""Tools the architect uses to keep the architecture draft current, and to propose
skipping the stage of an API change.

Not part of the chat TOOLSETS: the architecture and onboarding activities build these.
"""

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.tools import BaseTool, tool

from app.architecture.models import (
    ArchitectureStructure,
    architecture_problems,
    empty_structure,
)
from app.tools.api_design import JsonArg

logger = logging.getLogger(__name__)
MAX_CONFLICTS = 50


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _key(value: str) -> str:
    return _text(value).casefold()


@dataclass
class ArchitectureContext:
    """In-memory architecture for one turn; the workflow stores the result.

    `structure` starts as the draft so far, or as `base` (such as the repository's
    existing architecture) when there is none yet."""

    structure: ArchitectureStructure | None
    base: ArchitectureStructure | None = None
    # Whether propose_skip is offered at all.
    may_skip: bool = False
    skip_reason: str | None = None
    # Disagreements between the architecture and an existing contract, recorded by the
    # model of an architecture-only onboarding; None when not tracked.
    conflicts: list[str] | None = None
    # The task's requirements the architect assigns to its components, as plain records
    # with their components; None when the architect assigns none.
    requirements: list[dict[str, Any]] | None = None
    # Writes each assignment ({requirement ID: component}) to the task flow's
    # requirements store as it is made; None outside a task flow.
    persist: Callable[[dict[str, str | None]], Awaitable[None]] | None = None
    _changed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.structure = copy.deepcopy(self.structure)
        if self.requirements is not None:
            self.requirements = [dict(item) for item in self.requirements]

    def component_names(self) -> list[str]:
        structure = self.result()
        return [item["name"] for item in structure["components"]] if structure else []

    def unassigned(self) -> list[str]:
        """IDs of the requirements without one of the architecture's components."""
        names = set(self.component_names())
        return [str(item.get("id")) for item in self.requirements or []
                if item.get("component") not in names]

    def assign(self, requirement_ids: list[str], component: str) -> dict[str, Any]:
        """Assign the requirements to one of the architecture's components."""
        if self.requirements is None:
            raise ValueError("this session assigns no requirements.")
        if not self.component_names():
            raise ValueError("record the architecture's components first.")
        name = self._component(component)
        by_id = {str(item.get("id")).upper(): item for item in self.requirements}
        assigned, unknown = [], []
        for raw in requirement_ids:
            item = by_id.get(_text(raw).upper())
            if item is None:
                unknown.append(_text(raw))
                continue
            item["component"] = name
            assigned.append(str(item["id"]))
        if not assigned:
            raise ValueError("no known requirement ID given"
                             + (f" ({', '.join(unknown)})." if unknown else "."))
        return {"component": name, "assigned": assigned, "unknown": unknown}

    async def stored(self, assignment: dict[str, Any]) -> None:
        """Write an assignment to the requirements store; a store that cannot be written
        to does not stop the session, which keeps the assignment in its requirements."""
        if self.persist is None:
            return
        try:
            await self.persist({item: assignment["component"]
                                for item in assignment["assigned"]})
        except Exception as exc:  # noqa: BLE001 - kept in the session's requirements
            logger.warning("Component assignment not stored: %s: %s",
                           type(exc).__name__, exc)

    def _draft(self) -> ArchitectureStructure:
        if self.structure is None:
            self.structure = copy.deepcopy(self.base) if self.base else empty_structure()
        self._changed = True
        return self.structure

    def result(self) -> ArchitectureStructure | None:
        """The draft after the turn: None when nothing was ever recorded."""
        if self.structure is None and self._changed:
            return empty_structure()
        return self.structure

    def record_component(self, name: str, responsibilities: list[str]) -> dict[str, Any]:
        name = _text(name)
        if not name:
            raise ValueError("name cannot be empty.")
        listed = [_text(item) for item in responsibilities or [] if _text(item)]
        if not listed:
            raise ValueError("list at least one responsibility of the component.")
        draft = self._draft()
        component = {"name": name, "responsibilities": listed}
        for index, item in enumerate(draft["components"]):
            if _key(item["name"]) == _key(name):
                old = item["name"]
                draft["components"][index] = component  # type: ignore[call-overload]
                if old != name:
                    for flow in draft["data_flows"]:
                        for end in ("source", "target"):
                            if flow[end] == old:
                                flow[end] = name  # type: ignore[literal-required]
                return component
        draft["components"].append(component)  # type: ignore[arg-type]
        return component

    def remove_component(self, name: str) -> str:
        draft = self._draft()
        for item in draft["components"]:
            if _key(item["name"]) == _key(name):
                draft["components"].remove(item)
                draft["data_flows"] = [flow for flow in draft["data_flows"]
                                       if item["name"] not in (flow["source"], flow["target"])]
                return item["name"]
        raise ValueError(f"Unknown component {name!r}.")

    def _component(self, name: str) -> str:
        assert self.structure is not None
        for item in self.structure["components"]:
            if _key(item["name"]) == _key(name):
                return item["name"]
        raise ValueError(f"Unknown component {name!r}; record it with record_component first.")

    def record_data_flow(self, source: str, target: str, description: str) -> dict[str, Any]:
        draft = self._draft()
        source, target = self._component(source), self._component(target)
        if not _text(description):
            raise ValueError("description must say what data flows and how.")
        flow = {"source": source, "target": target, "description": _text(description)}
        for index, item in enumerate(draft["data_flows"]):
            if item["source"] == source and item["target"] == target:
                draft["data_flows"][index] = flow  # type: ignore[call-overload]
                return flow
        draft["data_flows"].append(flow)  # type: ignore[arg-type]
        return flow

    def remove_data_flow(self, source: str, target: str) -> str:
        draft = self._draft()
        for item in draft["data_flows"]:
            if _key(item["source"]) == _key(source) and _key(item["target"]) == _key(target):
                draft["data_flows"].remove(item)
                return f"{item['source']} → {item['target']}"
        raise ValueError(f"Unknown data flow from {source!r} to {target!r}.")

    def record_technology_choice(self, topic: str, choice: str,
                                 rationale: str = "") -> dict[str, Any]:
        topic, choice = _text(topic), _text(choice)
        if not topic or not choice:
            raise ValueError("topic and choice cannot be empty.")
        draft = self._draft()
        recorded: dict[str, Any] = {"topic": topic, "choice": choice}
        if _text(rationale):
            recorded["rationale"] = _text(rationale)
        for index, item in enumerate(draft["technology_choices"]):
            if _key(item["topic"]) == _key(topic):
                draft["technology_choices"][index] = recorded  # type: ignore[call-overload]
                return recorded
        draft["technology_choices"].append(recorded)  # type: ignore[arg-type]
        return recorded

    def remove_technology_choice(self, topic: str) -> str:
        draft = self._draft()
        for item in draft["technology_choices"]:
            if _key(item["topic"]) == _key(topic):
                draft["technology_choices"].remove(item)
                return item["topic"]
        raise ValueError(f"Unknown technology choice {topic!r}.")

    def problems(self) -> list[str]:
        return architecture_problems(self.result())


def build(ctx: ArchitectureContext) -> list[BaseTool]:
    def result(action: str, **values: Any) -> str:
        payload = {"action": action, **values, "problems": ctx.problems()}
        if ctx.requirements is not None and ctx.component_names():
            payload["requirements_without_component"] = ctx.unassigned()
        return json.dumps(payload, ensure_ascii=False)

    def guarded(action: str, call: Any) -> str:
        try:
            value = call()
        except ValueError as exc:
            logger.warning("Architecture not changed (%s): %s", action, exc)
            return f"Not {action}: {exc}"
        return result(action, value=value)

    @tool
    async def record_component(
        name: str, responsibilities: Annotated[list[str], JsonArg],
    ) -> str:
        """Add a component or module of the architecture, or replace the one with the
        same name. responsibilities: what the component is responsible for, one short
        statement each."""
        return guarded("recorded", lambda: ctx.record_component(name, responsibilities))

    @tool
    async def remove_component(name: str) -> str:
        """Remove a component, with the data flows from and to it."""
        return guarded("removed", lambda: ctx.remove_component(name))

    @tool
    async def record_data_flow(source: str, target: str, description: str) -> str:
        """Record the data one component passes to another, or replace the flow between
        the same two. source and target are names of recorded components; description
        says what data flows and how (such as a synchronous HTTP call or a queue)."""
        return guarded("recorded", lambda: ctx.record_data_flow(source, target, description))

    @tool
    async def remove_data_flow(source: str, target: str) -> str:
        """Remove the data flow from source to target."""
        return guarded("removed", lambda: ctx.remove_data_flow(source, target))

    @tool
    async def record_technology_choice(topic: str, choice: str, rationale: str = "") -> str:
        """Record a key technology choice, or replace the one of the same topic. topic is
        what it is about, such as storage, messaging or the web framework."""
        return guarded("recorded",
                       lambda: ctx.record_technology_choice(topic, choice, rationale))

    @tool
    async def remove_technology_choice(topic: str) -> str:
        """Remove the technology choice of a topic."""
        return guarded("removed", lambda: ctx.remove_technology_choice(topic))

    @tool
    async def show_architecture() -> str:
        """Show the whole architecture recorded so far and its open problems."""
        return json.dumps({"architecture": ctx.result(), "problems": ctx.problems()},
                          ensure_ascii=False)

    tools: list[BaseTool] = [record_component, remove_component, record_data_flow,
                             remove_data_flow, record_technology_choice,
                             remove_technology_choice, show_architecture]

    if ctx.requirements is not None:
        @tool
        async def assign_requirements(
            requirement_ids: Annotated[list[str], JsonArg], component: str,
        ) -> str:
            """Assign requirements to the recorded component responsible for them, or move
            them to another one. Every requirement must belong to one component: assign
            each one listed under requirements_without_component."""
            try:
                assignment = ctx.assign(requirement_ids, component)
            except ValueError as exc:
                logger.warning("Requirements not assigned: %s", exc)
                return f"Not assigned: {exc}"
            await ctx.stored(assignment)
            return result("assigned", value=assignment)

        @tool
        async def list_requirement_components() -> str:
            """List the task's requirements with the component each belongs to (null for
            none yet)."""
            return json.dumps({
                "requirements": [{"id": item.get("id"), "title": item.get("title"),
                                  "component": item.get("component")}
                                 for item in ctx.requirements or []],
                "requirements_without_component": ctx.unassigned(),
            }, ensure_ascii=False)

        tools += [assign_requirements, list_requirement_components]

    if ctx.may_skip:
        @tool
        async def propose_skip(reason: str) -> str:
            """Propose skipping the architecture stage instead of drafting, when the API
            change needs no new architecture: the repository's existing architecture then
            stays as it is. reason says why. The user confirms or declines; record nothing
            else in this turn."""
            if not _text(reason):
                return "Not proposed: give the reason why no new architecture is needed."
            ctx.skip_reason = _text(reason)
            return ("The proposal is shown to the user, who confirms or declines it. End the "
                    "turn now with a short reply explaining the proposal.")

        tools.append(propose_skip)

    if ctx.conflicts is not None:
        conflicts = ctx.conflicts

        @tool
        async def record_conflict(description: str) -> str:
            """Record something the architecture disagrees with in the existing api.json or
            requirements.json. Conflicts block finalizing until resolved."""
            text = _text(description)
            if not text:
                return "Not recorded: describe the disagreement."
            if text not in conflicts and len(conflicts) < MAX_CONFLICTS:
                conflicts.append(text)
            return json.dumps({"conflicts": conflicts}, ensure_ascii=False)

        @tool
        async def resolve_conflict(description: str) -> str:
            """Remove a recorded conflict once the architecture agrees with the existing
            contract on it. description is the conflict as recorded."""
            text = _text(description)
            if text not in conflicts:
                return (f"Not resolved: unknown conflict. Recorded: "
                        f"{json.dumps(conflicts, ensure_ascii=False)}")
            conflicts.remove(text)
            return json.dumps({"conflicts": conflicts}, ensure_ascii=False)

        tools += [record_conflict, resolve_conflict]
    return tools
