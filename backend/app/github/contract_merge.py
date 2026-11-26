"""The merge of a repository's contract (contracts/) with that of the branch merged in.

Two deliveries made side by side each write the whole contract of their repository: the
requirements they added, the API they changed, the architecture. When one is merged into
the other's branch, a merge that keeps one side's files loses what the other added, so
the contract files are merged by their content instead: requirements by ID, endpoints by
method and path, schemas and components by name, and text by keeping both sides' lines.
Where both sides changed the same value, `ours` (the branch merged into) wins."""
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.api_design.openapi import render_openapi
from app.requirements.document import render_requirements_markdown, requirements_narrative

REQUIREMENTS = 'requirements.json'
NARRATIVE = 'requirements.md'
API = 'api.json'
OPENAPI = 'openapi.yaml'
# Every file of the contract its other files are read with; the others merge on their own.
CONTRACT_FILES = (REQUIREMENTS, NARRATIVE, API, OPENAPI, 'architecture.json',
                  'architecture.md', 'language.json')

# The three versions of a file: where the branches parted, ours and theirs; None where a
# side has no such file.
Versions = tuple[str | None, str | None, str | None]
# Merges three texts, keeping the lines both sides added.
TextMerge = Callable[[str, str, str], Awaitable[str]]

_MISSING: Any = object()


def _identity(item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get('method'), str) and isinstance(item.get('path'), str):
        return ('endpoint', item['method'], item['path'])
    for key in ('id', 'name'):
        if isinstance(item.get(key), str):
            return (key, item[key])
    return None


def _keyed(items: list[Any]) -> dict[Any, Any] | None:
    """The items by their identity, when every one has a distinct one."""
    keyed: dict[Any, Any] = {}
    for item in items:
        key = _identity(item)
        if key is None or key in keyed:
            return None
        keyed[key] = item
    return keyed


def merge_values(base: Any, ours: Any, theirs: Any) -> Any:
    """The three-way merge of two JSON values; _MISSING stands for an absent one."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        parted = base if isinstance(base, dict) else {}
        merged: dict[str, Any] = {}
        for key in [*ours, *(key for key in theirs if key not in ours)]:
            value = merge_values(parted.get(key, _MISSING), ours.get(key, _MISSING),
                                 theirs.get(key, _MISSING))
            if value is not _MISSING:
                merged[key] = value
        return merged
    if isinstance(ours, list) and isinstance(theirs, list):
        parted_list = base if isinstance(base, list) else []
        mine, other, earlier = _keyed(ours), _keyed(theirs), _keyed(parted_list)
        if mine is not None and other is not None and earlier is not None:
            result = []
            for key in [*mine, *(key for key in other if key not in mine)]:
                value = merge_values(earlier.get(key, _MISSING), mine.get(key, _MISSING),
                                     other.get(key, _MISSING))
                if value is not _MISSING:
                    result.append(value)
            return result
        # Plain lists, such as requirement IDs: what either side added or removed.
        removed = [item for item in parted_list if item not in theirs]
        kept = [item for item in ours if item not in removed]
        return kept + [item for item in theirs
                       if item not in parted_list and item not in kept]
    # Both changed the value: this branch's change wins.
    return ours


def _parse(text: str | None) -> Any:
    if text is None:
        return _MISSING
    return json.loads(text)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _number(identifier: str) -> tuple[str, int] | None:
    found = re.fullmatch(r'(.*?)(\d+)', identifier)
    return (found.group(1), int(found.group(2))) if found else None


def merge_requirements(base: list[Any], ours: list[Any],
                       theirs: list[Any]) -> tuple[list[Any], dict[str, str]]:
    """The requirements of both sides, and the new IDs of theirs: a requirement both
    added under the same ID, as two deliveries made side by side number theirs after the
    same contract, is kept twice, theirs numbered after the highest ID of its prefix."""
    parted = {item.get('id') for item in base if isinstance(item, dict)}
    mine = {item.get('id'): item for item in ours if isinstance(item, dict)}
    taken = {str(item.get('id')) for item in [*ours, *theirs, *base] if isinstance(item, dict)}
    renamed: dict[str, str] = {}
    renumbered = []
    for item in theirs:
        identifier = item.get('id') if isinstance(item, dict) else None
        if (isinstance(identifier, str) and identifier not in parted and identifier in mine
                and mine[identifier] != item and _number(identifier) is not None):
            prefix, _ = _number(identifier)  # type: ignore[misc]
            highest = max((parsed[1] for parsed in map(_number, taken)
                           if parsed is not None and parsed[0] == prefix), default=0)
            fresh = f'{prefix}{highest + 1}'
            taken.add(fresh)
            renamed[identifier] = fresh
            item = {**item, 'id': fresh}
        renumbered.append(item)
    return merge_values(base, ours, renumbered), renamed


def _renamed_api(api: Any, renamed: dict[str, str]) -> Any:
    if not renamed or not isinstance(api, dict) or not isinstance(api.get('endpoints'), list):
        return api
    return {**api, 'endpoints': [
        {**endpoint, 'requirement_ids': [renamed.get(item, item)
                                         for item in endpoint['requirement_ids']]}
        if isinstance(endpoint, dict) and isinstance(endpoint.get('requirement_ids'), list)
        else endpoint for endpoint in api['endpoints']]}


async def merge_contract(versions: dict[str, Versions], conflicting: set[str],
                         merge_text: TextMerge) -> dict[str, str | None]:
    """The content of each `conflicting` file of the contract directory (by its name
    there) once both sides' contracts are merged, and of the files rendered from them;
    None for a file to delete. `versions` holds the three versions of CONTRACT_FILES and
    of the conflicting files."""
    results: dict[str, str | None] = {}

    def three(name: str) -> tuple[Any, Any, Any] | None:
        base, ours, theirs = versions.get(name, (None, None, None))
        try:
            parsed = (_parse(base), _parse(ours), _parse(theirs))
        except ValueError:
            return None
        return parsed

    requirements: list[Any] | None = None
    renamed: dict[str, str] = {}
    parsed = three(REQUIREMENTS)
    if parsed is not None and all(isinstance(value, list) or value is _MISSING
                                  for value in parsed):
        base, ours, theirs = (value if value is not _MISSING else [] for value in parsed)
        if parsed[1] is not _MISSING or parsed[2] is not _MISSING:
            requirements, renamed = merge_requirements(base, ours, theirs)
    api: Any = None
    parsed = three(API)
    if parsed is not None and (parsed[1] is not _MISSING or parsed[2] is not _MISSING):
        merged = merge_values(parsed[0], parsed[1], _renamed_api(parsed[2], renamed))
        if isinstance(merged, dict):
            api = merged
    if requirements is not None and (REQUIREMENTS in conflicting or renamed
                                     or NARRATIVE in conflicting):
        results[REQUIREMENTS] = _dump(requirements)
        narratives = []
        for document, listed in zip(versions.get(NARRATIVE, (None, None, None)),
                                    (three(REQUIREMENTS) or (_MISSING,) * 3)):
            narrative = (requirements_narrative(document, listed)
                         if document is not None and isinstance(listed, list) else None)
            narratives.append(narrative)
        base_text, ours_text, theirs_text = narratives
        if ours_text is not None and theirs_text is not None:
            narrative = await merge_text(base_text or '', ours_text, theirs_text)
            try:
                results[NARRATIVE] = render_requirements_markdown(requirements,
                                                                  narrative.strip() or None)
            except (KeyError, TypeError, AttributeError, ValueError):
                pass
    if api is not None and (API in conflicting or OPENAPI in conflicting or renamed):
        results[API] = _dump(api)
        try:
            results[OPENAPI] = render_openapi(api)
        except (KeyError, TypeError, AttributeError, ValueError):
            pass
    for name in sorted(conflicting - set(results)):
        base, ours, theirs = versions.get(name, (None, None, None))
        if ours is None or theirs is None:
            # Removed on one side, changed on the other: the change is kept.
            results[name] = ours if ours is not None else theirs
            continue
        if name.endswith('.json'):
            parsed = three(name)
            if parsed is not None:
                merged = merge_values(*parsed)
                results[name] = None if merged is _MISSING else _dump(merged)
                continue
        results[name] = await merge_text(base or '', ours, theirs)
    return results
