"""The requirements a requirement ID mentioned in a conversation can refer to, which the
session responses list as lookup_requirements for the web client to explain on hover or
tap: those recorded in the session, the existing service's and those the task retires."""

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypedDict

import psycopg

from app.asyncio_utils import run_sync
from app.config import GENERATED_CODE_DIR
from app.delivery.contract import ContractError, read_service_contract
from app.github.checkout import CheckoutError, checkout_path
from app.requirements import store as requirements_store
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
    # The architecture component it is grouped under; None while it has none.
    component: str | None
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
    component = item.get("component")
    return {
        "id": requirement_id,
        "category": category,
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "priority": priority,
        "component": str(component) if component else None,
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


async def stored_lookup(task_flow_id: str | None, own_only: bool = False,
                        ) -> list[LookupRequirement] | None:
    """The lookup list of a session of task flow `task_flow_id`, resolved from the
    requirements store: the flow's baseline (the existing service's requirements) overlaid
    by its own changes, those it retires marked retired; with `own_only`, for a session
    without an existing service, only those the flow recorded. None when the store does
    not know the flow or cannot be read, so the caller resolves it as before."""
    if not task_flow_id:
        return None
    try:
        # A flow whose branch was never synced, such as when the store was unreachable
        # as it started, cannot list the existing service's requirements from it.
        stored = await requirements_store.read(task_flow_id, own_only=own_only,
                                               require_baseline=not own_only)
    except (OSError, psycopg.Error) as exc:
        logger.warning("Requirements of task flow %s unavailable for the lookup: %s",
                       task_flow_id, exc)
        return None
    if stored is None:
        return None
    entries: dict[str, LookupRequirement] = {}
    for item in stored:
        entry = _entry(item, item["retired"])
        if entry is not None:
            entries[entry["id"]] = entry
    return list(entries.values())


async def requirements_session_lookup(session_id: str, state: Any) -> list[LookupRequirement]:
    """The lookup list of a requirements session (RequirementsState): from the
    requirements store of its task flow, else from its state and checkout."""
    stored = await stored_lookup(getattr(state, "task_flow_id", None),
                                 own_only=state.kind == "new_service")
    if stored is not None:
        return stored
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
                                *, task_flow_id: str | None = None,
                                kind: str | None = None,
                                ) -> list[LookupRequirement]:
    """The lookup list of a session derived from a requirements session, such as an
    architecture or API design session: from the requirements store of its task flow,
    else that of its requirements session, or the derived session's own requirements
    when that cannot be read."""
    stored = await stored_lookup(task_flow_id, own_only=kind == "new_service")
    if stored is not None:
        return stored
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
