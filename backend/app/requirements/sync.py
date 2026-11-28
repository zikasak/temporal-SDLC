"""Sync a branch's contracts/requirements.json into the requirements store's baseline
when a requirements or onboarding flow starts, inferring the component of each
requirement without one from the component responsibilities in contracts/architecture.json.
Runs in worker activities only."""

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.asyncio_utils import run_sync
from app.requirements import store

logger = logging.getLogger(__name__)

CONTRACTS_DIR = "contracts"
REQUIREMENTS_FILENAME = "requirements.json"
ARCHITECTURE_JSON_FILENAME = "architecture.json"
# How long the component inference may take before the components are left empty.
INFERENCE_SECONDS = 180
# How many requirements one inference call assigns.
INFERENCE_BATCH = 80

INFERENCE_PROMPT = """You assign software requirements to the architecture components
responsible for them. The components, each with its responsibilities:

{components}

The requirements:

{requirements}

Reply with one JSON object and nothing else: for each requirement ID, the name of the one
component above whose responsibilities cover it best, written exactly as above, or null
when none of them fits. Example: {{"FR-1": "HTTP API", "NFR-2": null}}"""


def read_branch_requirements(checkout: Path) -> list[dict[str, Any]]:
    """The requirements of the checkout's contracts/requirements.json; none when it has
    none or it is unreadable."""
    try:
        data = json.loads((checkout / CONTRACTS_DIR / REQUIREMENTS_FILENAME).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data
            if isinstance(item, dict) and store.canonical_id(item.get("id"))]


def read_components(checkout: Path) -> list[dict[str, Any]]:
    """The components of the checkout's contracts/architecture.json, with their
    responsibilities; none when it has none or it is unreadable."""
    from app.architecture.models import normalize_structure
    try:
        structure = normalize_structure(json.loads(
            (checkout / CONTRACTS_DIR / ARCHITECTURE_JSON_FILENAME).read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        return []
    return [dict(item) for item in structure["components"]]


def component_names(checkout: Path) -> tuple[str, ...]:
    return tuple(item["name"] for item in read_components(checkout))


def _json_object(text: str) -> dict[str, Any]:
    """The first JSON object in the model's reply."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def infer_components(requirements: Sequence[Mapping[str, Any]],
                           components: Sequence[Mapping[str, Any]],
                           ) -> dict[str, str | None]:
    """The component of each requirement by its ID, matched by an LLM (the requirements
    interviewer's model settings) to the components' responsibilities; null where none
    fits or the model could not say."""
    if not requirements or not components:
        return {}
    from langchain_core.messages import HumanMessage

    from app.llm.graph import native_session
    from app.llm.model import get_model, one_shot

    names = {str(item["name"]).casefold(): str(item["name"]) for item in components}
    listed = "\n".join(
        f"- {item['name']}: " + "; ".join(item.get("responsibilities") or [])
        for item in components)
    model = one_shot(await get_model("requirements"))
    result: dict[str, str | None] = {}
    for start in range(0, len(requirements), INFERENCE_BATCH):
        batch = requirements[start:start + INFERENCE_BATCH]
        text = "\n".join(
            f"- {store.canonical_id(item.get('id'))}: {item.get('title', '')}. "
            f"{item.get('description', '')}" for item in batch)
        async with native_session(model):
            reply = await model.ainvoke([HumanMessage(content=INFERENCE_PROMPT.format(
                components=listed, requirements=text))])
        answer = _json_object(str(getattr(reply, "text", None)
                                  or getattr(reply, "content", "") or ""))
        for item in batch:
            requirement_id = store.canonical_id(item.get("id"))
            if requirement_id is None:
                continue
            chosen = answer.get(requirement_id) or answer.get(str(item.get("id")))
            result[requirement_id] = (names.get(str(chosen).strip().casefold())
                                      if isinstance(chosen, str) else None)
    return result


def _same_text(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (str(a.get("title") or "").strip() == str(b.get("title") or "").strip()
            and str(a.get("description") or "").strip()
            == str(b.get("description") or "").strip())


async def with_components(requirements: list[dict[str, Any]],
                          components: list[dict[str, Any]],
                          stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The branch's requirements, each with its component: the repository's where it
    has a component field; else the one inferred earlier for the same text, while that
    component still exists; else one inferred now from the architecture's components,
    null when there are none or none fits."""
    names = {str(item["name"]) for item in components}
    earlier = {row["id"]: row for row in stored}
    result: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in requirements:
        record = dict(item)
        requirement_id = store.canonical_id(item.get("id"))
        if "component" not in item:
            before = earlier.get(requirement_id or "")
            if (before is not None and before.get("component") in names
                    and _same_text(before, item)):
                record["component"] = before["component"]
            else:
                record["component"] = None
                if names:
                    missing.append(record)
        result.append(record)
    if missing:
        try:
            async with asyncio.timeout(INFERENCE_SECONDS):
                inferred = await infer_components(missing, components)
        except Exception as exc:  # noqa: BLE001 - the components then stay empty
            logger.warning("Could not infer the components of %d requirements: %s: %s",
                           len(missing), type(exc).__name__, exc)
            inferred = {}
        for record in missing:
            record["component"] = inferred.get(store.canonical_id(record.get("id")) or "")
    return result


# How alike the title and description of a requirement must be to the version under the
# same ID after a merge to count as the same requirement.
SIMILARITY = 0.6


def _text(item: Mapping[str, Any]) -> str:
    return " ".join(f"{item.get('title') or ''} {item.get('description') or ''}"
                    .casefold().split())


def map_requirements(own: Sequence[Mapping[str, Any]], merged: Sequence[Mapping[str, Any]],
                     ) -> tuple[dict[str, str], list[str]]:
    """Map each of a flow's own requirements to its counterpart among the `merged`
    contract's requirements after merge conflicts were resolved: the requirement under
    the same ID when its content is similar, else one with the same title and
    description under another ID. Returns the renumbered IDs (old to new) and the IDs of
    those with no counterpart, which only the user can map."""
    from difflib import SequenceMatcher

    by_id = {store.canonical_id(item.get("id")): item for item in merged}
    by_text: dict[str, str] = {}
    for item in merged:
        requirement_id = store.canonical_id(item.get("id"))
        if requirement_id:
            by_text.setdefault(_text(item), requirement_id)
    renumbered: dict[str, str] = {}
    unmatched: list[str] = []
    for item in own:
        requirement_id = store.canonical_id(item.get("id"))
        if requirement_id is None:
            continue
        same = by_id.get(requirement_id)
        if same is not None and SequenceMatcher(
                None, _text(item), _text(same)).ratio() >= SIMILARITY:
            continue
        moved = by_text.get(_text(item))
        if moved is not None and moved != requirement_id:
            renumbered[requirement_id] = moved
        else:
            unmatched.append(requirement_id)
    return renumbered, unmatched


# How many merged requirements a mapping question offers as counterparts.
MAPPING_CANDIDATES = 4
MAPPING_TITLE_CHARS = 120
# The answers of a mapping question that keep the requirement as it is, or remove it.
KEEP_ANSWER = "Keep it under {id}"
REMOVE_ANSWER = "Remove it"
_ANSWER_ID = re.compile(r"\b([A-Za-z]+-\d+)\b")
_REMOVAL_WORDS = ("remove", "delete", "drop")


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def mapping_questions(own: Sequence[Mapping[str, Any]], merged: Sequence[Mapping[str, Any]],
                      unmatched: Sequence[str], renumbered: Mapping[str, str] | None = None,
                      ) -> list[dict[str, Any]]:
    """What the user is asked about each of the flow's requirements in `unmatched` (those
    `map_requirements` found no counterpart for): which requirement of the merged
    contract it corresponds to. Each entry has the requirement's `id`, its `title`, the
    `question` and its `options`: the most similar merged requirements no other of the
    flow's requirements maps to, keeping it under its ID, or removing it."""
    from difflib import SequenceMatcher

    own_by_id = {store.canonical_id(item.get("id")): item for item in own}
    claimed = set((renumbered or {}).values()) | {
        requirement_id for requirement_id in own_by_id
        if requirement_id and requirement_id not in unmatched}
    entries: list[dict[str, Any]] = []
    for requirement_id in unmatched:
        item = own_by_id.get(requirement_id)
        if item is None:
            continue
        text = _text(item)
        scored = []
        for candidate in merged:
            candidate_id = store.canonical_id(candidate.get("id"))
            if candidate_id is None or candidate_id in claimed:
                continue
            ratio = SequenceMatcher(None, text, _text(candidate)).ratio()
            scored.append((-ratio, candidate_id, str(candidate.get("title") or "")))
        scored.sort()
        options = [f"{candidate_id}: {_short(title, MAPPING_TITLE_CHARS)}" if title
                   else candidate_id for _, candidate_id, title in scored[:MAPPING_CANDIDATES]]
        options += [KEEP_ANSWER.format(id=requirement_id), REMOVE_ANSWER]
        title = _short(str(item.get("title") or ""), MAPPING_TITLE_CHARS)
        entries.append({
            "id": requirement_id, "title": title,
            "question": (
                f"After the base branch was merged, the requirement {requirement_id}"
                f"{f' ({title})' if title else ''} of this change no longer matches the "
                "merged contracts/requirements.json. Which requirement does it "
                "correspond to?"),
            "options": options,
        })
    return entries


def answer_mapping(entries: Sequence[Mapping[str, Any]], answers: Sequence[str],
                   ) -> tuple[dict[str, str], list[str]]:
    """The renumbering (old to new ID) and the removals the user's answers to the
    `mapping_questions` decide, in the same order. An answer naming another requirement
    ID renumbers the requirement to it; one asking to remove it removes it; any other
    answer keeps it under its ID."""
    renumbered: dict[str, str] = {}
    removed: list[str] = []
    for entry, answer in zip(entries, answers, strict=False):
        requirement_id = str(entry["id"])
        text = str(answer or "").strip()
        if not text or text == KEEP_ANSWER.format(id=requirement_id):
            continue
        if text == REMOVE_ANSWER:
            removed.append(requirement_id)
            continue
        named = [store.canonical_id(match) for match in _ANSWER_ID.findall(text)]
        target = next((value for value in named if value and value != requirement_id), None)
        if target is not None:
            renumbered[requirement_id] = target
        elif any(word in text.casefold() for word in _REMOVAL_WORDS):
            removed.append(requirement_id)
    return renumbered, removed


async def sync_branch(task_flow_id: str, repository: str, branch: str,
                      checkout: Path) -> int:
    """Sync the checkout's contracts/requirements.json into the baseline of `repository`
    and `branch`, and link the task flow to it. Returns how many requirements the
    baseline holds."""
    requirements = await run_sync(read_branch_requirements, checkout)
    components = await run_sync(read_components, checkout)
    stored = await store.baseline(repository, branch)
    synced = await with_components(requirements, components, stored)
    count = await store.sync_baseline(repository, branch, synced)
    await store.link_flow(task_flow_id, repository, branch)
    logger.info("Synced %d requirements of %s@%s for task flow %s", count, repository,
                branch, task_flow_id)
    return count
