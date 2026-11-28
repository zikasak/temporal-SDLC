"""The requirements a requirement ID mentioned in a conversation can refer to, which the
session responses list as lookup_requirements for the web client to explain on hover or
tap: those recorded in the session, the existing service's and those the task retires."""

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypedDict

from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.delivery.contract import ContractError, read_service_contract
from app.github.checkout import CheckoutError, checkout_path
from app.requirements.models import CATEGORY_PREFIXES, PRIORITIES

logger = logging.getLogger(__name__)

# A requirement ID in any letter case, with or without the dash: FR-12, fr12.
ID_PATTERN = re.compile(r"([A-Za-z]+)-?(\d+)")
_PREFIX_CATEGORIES = {prefix: category for category, prefix in CATEGORY_PREFIXES.items()}


class LookupRequirement(TypedDict):
    id: str
    category: str
    title: str
    description: str
    priority: str
    retired: bool


def canonical_id(value: Any) -> str | None:
    """The ID as PREFIX-NUMBER with an upper-case prefix; None for no requirement ID."""
    match = ID_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        return None
    return f"{match.group(1).upper()}-{match.group(2)}"


def _entry(item: Any, retired: bool) -> LookupRequirement | None:
    if not isinstance(item, dict):
        return None
    requirement_id = canonical_id(item.get("id"))
    if requirement_id is None:
        return None
    category = item.get("category")
    if category not in CATEGORY_PREFIXES:
        category = _PREFIX_CATEGORIES.get(requirement_id.partition("-")[0], "functional")
    priority = item.get("priority")
    if priority not in PRIORITIES:
        priority = "must"
    return {
        "id": requirement_id,
        "category": category,
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "priority": priority,
        "retired": retired,
    }


def lookup_requirements(recorded: Iterable[Any] | None,
                        existing: Iterable[Any] | None = None,
                        retired: Iterable[Any] | None = None) -> list[LookupRequirement]:
    """One entry per ID: the existing service's requirements, replaced by the session's
    version of the same ID, and those the task retires marked retired."""
    entries: dict[str, LookupRequirement] = {}
    for items, is_retired in ((existing, False), (recorded, False), (retired, True)):
        for item in items or ():
            entry = _entry(item, is_retired)
            if entry is not None:
                entries[entry["id"]] = entry
    return list(entries.values())


# The polled responses read the same contract again and again; it is read once per change.
_contract_cache: dict[Path, tuple[int, tuple[Any, ...]]] = {}
_CACHE_SIZE = 256


def _read_checkout_requirements(session_id: str) -> tuple[Any, ...]:
    try:
        root = checkout_path(GENERATED_CODE_DIR, session_id)
    except CheckoutError:
        return ()
    marker = root / "contracts" / "requirements.json"
    try:
        stamp = marker.stat().st_mtime_ns
    except OSError:
        return ()
    cached = _contract_cache.get(root)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        requirements = tuple(read_service_contract(root)["requirements"])
    except (ContractError, OSError):
        requirements = ()
    if len(_contract_cache) >= _CACHE_SIZE:
        _contract_cache.clear()
    _contract_cache[root] = (stamp, requirements)
    return requirements


async def checkout_requirements(session_id: str) -> list[Any]:
    """The requirements of the contract in the session's checkout; none when the
    checkout is not on this host's disk or has no readable contract."""
    try:
        return list(await run_sync(_read_checkout_requirements, session_id))
    except Exception:  # noqa: BLE001 - the lookup is an aid; the session is still shown
        logger.warning("Could not read the contract of session %s", session_id,
                       exc_info=True)
        return []


async def stored_requirements(task_flow_id: str | None) -> list[dict[str, Any]]:
    """The requirements stored in the database for the task flow, as dicts with the keys
    of app.requirements.models.Requirement plus "component" (str or None); an empty list
    when `task_flow_id` is None."""
    return []


class RequirementStore:
    """The database table of requirements, scoped per task flow and linked to a
    repository and branch. A requirement is a dict with the keys of
    app.requirements.models.Requirement plus "component" (str or None)."""

    async def list_requirements(self, flow_id: str) -> list[dict[str, Any]]:
        """Every requirement stored for `flow_id`, and no other flow's."""
        return []

    async def save_requirement(self, flow_id: str, requirement: dict[str, Any],
                               repository: str, branch: str) -> None:
        """Insert or replace (by id) the flow's requirement, linked to the repository
        (full name, "owner/name") and branch."""
        return None

    async def delete_requirement(self, flow_id: str, requirement_id: str) -> None:
        """Remove the flow's requirement with that id."""
        return None


def infer_component(requirement: dict[str, Any],
                    architecture: dict[str, Any] | None) -> str | None:
    """The name of the component of `architecture` (the structure of
    contracts/architecture.json) whose name and responsibilities share the most words
    with the requirement's title and description; None when none can be inferred."""
    return None


async def sync_repository_requirements(store: RequirementStore, flow_id: str,
                                       repository: str, branch: str,
                                       repository_requirements: list[dict[str, Any]],
                                       architecture: dict[str, Any] | None) -> None:
    """Called when a new requirements flow starts: store every requirement of the
    branch's repository contract for `flow_id`, the repository's version overwriting."""
    return None


async def record_requirement(store: RequirementStore, flow_id: str, repository: str,
                             branch: str, requirement: dict[str, Any],
                             components: list[str]) -> dict[str, Any]:
    """Record or update a requirement of the flow as the requirements interviewer does
    and return the stored requirement."""
    return dict(requirement)


async def remove_requirement(store: RequirementStore, flow_id: str,
                             requirement_id: str) -> None:
    """Remove a requirement of the flow from the store immediately."""
    return None


async def read_requirements(store: RequirementStore, flow_id: str,
                            component: str | None = None,
                            requirement_id: str | None = None,
                            query: str | None = None) -> list[dict[str, Any]]:
    """The requirements of `flow_id` only, filtered by component, requirement ID and
    free-text query, ordered by id."""
    return []


async def assign_components(store: RequirementStore, flow_id: str, repository: str,
                            branch: str, components: list[str],
                            assignments: dict[str, str]) -> list[str]:
    """Called once the architecture stage has defined `components`: assign every stored
    requirement of the flow without a component; returns the ids so assigned."""
    return []


def map_requirements(previous: list[dict[str, Any]],
                     current: list[dict[str, Any]]) -> dict[str, Any]:
    """Used when merge conflicts are resolved and requirements were changed or
    renumbered. Returns {"mapped": {current_id: previous_id}, "ask": [current_id, ...]}."""
    return {"mapped": {}, "ask": []}


async def copy_created_requirements(store: RequirementStore, source_flow_id: str,
                                    target_flow_id: str, repository: str, branch: str,
                                    created_ids: list[str]) -> None:
    """When a session is forked: copy the stored requirements of `source_flow_id` whose
    ids are in `created_ids` to `target_flow_id`."""
    return None


async def requirements_session_lookup(session_id: str, state: Any) -> list[LookupRequirement]:
    """The lookup list of a requirements session (RequirementsState)."""
    existing: list[Any] = []
    if state.kind != "new_service":
        if state.contract:
            existing = list(state.contract.get("requirements") or [])
        else:
            existing = await checkout_requirements(session_id)
    return lookup_requirements(state.requirements, existing, state.retired)


async def source_session_lookup(source_session_id: str | None,
                                fallback: Iterable[Any],
                                fallback_existing: Iterable[Any] | None = None,
                                ) -> list[LookupRequirement]:
    """The lookup list of a session derived from a requirements session, such as an
    architecture or API design session: that of its requirements session, or the
    derived session's own requirements when that cannot be read."""
    if source_session_id:
        # Imported here: the service imports the delivery service, which imports this.
        from app.requirements import service as requirements_service
        try:
            source = await requirements_service.get_state(source_session_id)
        except Exception:  # noqa: BLE001 - a requirements session gone long ago
            logger.info("Requirements session %s unavailable for the lookup",
                        source_session_id)
        else:
            return await requirements_session_lookup(source_session_id, source)
    return lookup_requirements(fallback, fallback_existing)
