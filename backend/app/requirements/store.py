"""The requirements store: a project's requirements in PostgreSQL, in two layers.

- The baseline of a repository and branch holds the requirements of the branch's
  contracts/requirements.json. It is synced from the branch when a requirements (or
  onboarding) flow starts, the repository's version overwriting the stored one, and is
  not versioned: flows already running on the branch see the updated baseline at once.
- The layer of a task flow holds the flow's own changes on top of its baseline:
  requirements it created or changed, removed or retired, and components it assigned to
  baseline requirements. A flow reads its baseline overlaid by its own layer, never the
  layer of another flow.

Written by worker activities (never from workflow code) as each change happens, and read
by the requirement lookups of the session responses and the read-requirements tool.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict

import psycopg

from app.db import connect

logger = logging.getLogger(__name__)

# What a flow's layer row says about its requirement.
CREATED = "created"      # recorded by the flow, with an ID its baseline does not have
CHANGED = "changed"      # recorded by the flow under an ID its baseline has
REMOVED = "removed"      # a baseline requirement the flow removed
RETIRED = "retired"      # a baseline requirement the flow's change ends
ASSIGNED = "assigned"    # a baseline requirement the flow only assigned a component to
BASELINE = "baseline"    # (read results only) the baseline's requirement as it is
Origin = Literal["baseline", "created", "changed", "retired", "assigned"]
# Rows the interview of a flow writes as a whole each time its requirements change.
SESSION_CHANGES = (CREATED, CHANGED, RETIRED)
# The flow's own requirements: those a session without an existing service lists.
OWN_ORIGINS = (CREATED, CHANGED)
CATEGORY_PREFIXES = {"functional": "FR", "non_functional": "NFR", "technical": "TR"}
PRIORITIES = ("must", "should", "could")
_ID = re.compile(r"([A-Za-z]+)-?(\d+)")
# The component filter value that selects requirements without a component.
UNASSIGNED = ("none", "null", "unassigned")
# How many task flows one cleanup run looks at.
CLEANUP_BATCH = 500


class StoredRequirement(TypedDict):
    id: str
    category: str
    title: str
    description: str
    priority: str
    rationale: NotRequired[str]
    component: str | None
    # Where the flow's version comes from: its baseline, or its own layer.
    origin: Origin
    retired: bool


def canonical_id(value: Any) -> str | None:
    match = _ID.fullmatch(str(value or "").strip())
    if not match:
        return None
    return f"{match.group(1).upper()}-{match.group(2)}"


def _row(item: Mapping[str, Any], position: int) -> dict[str, Any] | None:
    """The stored columns of a requirement record; None for one without a usable ID."""
    requirement_id = canonical_id(item.get("id"))
    if requirement_id is None:
        return None
    category = item.get("category")
    if category not in CATEGORY_PREFIXES:
        prefix = requirement_id.partition("-")[0]
        category = next((name for name, value in CATEGORY_PREFIXES.items()
                         if value == prefix), "functional")
    priority = item.get("priority")
    if priority not in PRIORITIES:
        priority = "must"
    component = item.get("component")
    rationale = item.get("rationale")
    return {
        "id": requirement_id, "category": category,
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "priority": priority,
        "rationale": str(rationale) if rationale else None,
        "component": (str(component).strip() or None) if component else None,
        "position": position,
    }


def _order(requirement_id: str) -> tuple[int, int, str]:
    match = _ID.fullmatch(requirement_id)
    prefixes = list(CATEGORY_PREFIXES.values())
    if match and match.group(1) in prefixes:
        return prefixes.index(match.group(1)), int(match.group(2)), requirement_id
    return len(prefixes), 0, requirement_id


def _stored(row: Mapping[str, Any]) -> StoredRequirement:
    item: StoredRequirement = {
        "id": row["id"], "category": row["category"], "title": row["title"],
        "description": row["description"], "priority": row["priority"],
        "component": row["component"], "origin": row["origin"],
        "retired": row["origin"] == RETIRED,
    }
    if row.get("rationale"):
        item["rationale"] = row["rationale"]
    return item


def as_requirement(item: StoredRequirement) -> dict[str, Any]:
    """A stored requirement as a contract's requirement record, with its component."""
    record = {key: item[key] for key in ("id", "category", "title", "description",  # type: ignore[literal-required]
                                         "priority")}
    if item.get("rationale"):
        record["rationale"] = item["rationale"]  # type: ignore[typeddict-item]
    record["component"] = item.get("component")
    return record


async def ensure_schema() -> None:
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.requirement_baselines (
                repository text NOT NULL,
                branch text NOT NULL,
                id text NOT NULL,
                position integer NOT NULL DEFAULT 0,
                category text NOT NULL,
                title text NOT NULL,
                description text NOT NULL,
                priority text NOT NULL,
                rationale text,
                component text,
                synced_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (repository, branch, id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.requirement_flows (
                task_flow_id text PRIMARY KEY,
                repository text,
                branch text,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.requirement_changes (
                task_flow_id text NOT NULL
                    REFERENCES app.requirement_flows (task_flow_id) ON DELETE CASCADE,
                id text NOT NULL,
                change text NOT NULL,
                position integer NOT NULL DEFAULT 0,
                category text NOT NULL,
                title text NOT NULL,
                description text NOT NULL,
                priority text NOT NULL,
                rationale text,
                component text,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (task_flow_id, id)
            )
        """)


async def _ensure_flow(conn: psycopg.AsyncConnection[Any], task_flow_id: str) -> None:
    await conn.execute(
        "INSERT INTO app.requirement_flows (task_flow_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (task_flow_id,))


async def link_flow(task_flow_id: str, repository: str, branch: str) -> None:
    """Link the task flow to the baseline of `repository` (owner/name) and `branch`."""
    async with await connect() as conn:
        await conn.execute(
            "INSERT INTO app.requirement_flows (task_flow_id, repository, branch) "
            "VALUES (%s, %s, %s) ON CONFLICT (task_flow_id) DO UPDATE "
            "SET repository = EXCLUDED.repository, branch = EXCLUDED.branch",
            (task_flow_id, repository, branch))


async def flow_baseline(task_flow_id: str) -> tuple[str, str] | None:
    """The repository and branch whose baseline the task flow overlays; None for a flow
    the store does not know or that is linked to none."""
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT repository, branch FROM app.requirement_flows WHERE task_flow_id = %s",
            (task_flow_id,))
        row = await cursor.fetchone()
    if row is None or row["repository"] is None:
        return None
    return row["repository"], row["branch"]


async def baseline(repository: str, branch: str) -> list[dict[str, Any]]:
    """The branch's stored baseline, in the order of its contract."""
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT id, category, title, description, priority, rationale, component "
            "FROM app.requirement_baselines WHERE repository = %s AND branch = %s "
            "ORDER BY position, id", (repository, branch))
        return [dict(row) for row in await cursor.fetchall()]


async def sync_baseline(repository: str, branch: str,
                        requirements: Iterable[Mapping[str, Any]]) -> int:
    """Make the branch's baseline exactly `requirements` (the branch's contract, with
    components): what the repository holds overwrites what was stored, and a stored
    requirement the repository no longer has goes. Returns how many it holds."""
    rows = [row for index, item in enumerate(requirements)
            if (row := _row(item, index)) is not None]
    async with await connect() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM app.requirement_baselines WHERE repository = %s AND branch = %s "
                "AND NOT (id = ANY(%s))", (repository, branch, [row["id"] for row in rows]))
            for row in rows:
                await conn.execute(
                    "INSERT INTO app.requirement_baselines (repository, branch, id, position, "
                    "category, title, description, priority, rationale, component, synced_at) "
                    "VALUES (%(repository)s, %(branch)s, %(id)s, %(position)s, %(category)s, "
                    "%(title)s, %(description)s, %(priority)s, %(rationale)s, %(component)s, "
                    "now()) ON CONFLICT (repository, branch, id) DO UPDATE SET "
                    "position = EXCLUDED.position, category = EXCLUDED.category, "
                    "title = EXCLUDED.title, description = EXCLUDED.description, "
                    "priority = EXCLUDED.priority, rationale = EXCLUDED.rationale, "
                    "component = EXCLUDED.component, synced_at = now()",
                    {**row, "repository": repository, "branch": branch})
    return len(rows)


async def _baseline_ids(conn: psycopg.AsyncConnection[Any], task_flow_id: str) -> set[str]:
    cursor = await conn.execute(
        "SELECT b.id FROM app.requirement_baselines b JOIN app.requirement_flows f "
        "ON f.repository = b.repository AND f.branch = b.branch WHERE f.task_flow_id = %s",
        (task_flow_id,))
    return {row["id"] for row in await cursor.fetchall()}


async def _upsert(conn: psycopg.AsyncConnection[Any], task_flow_id: str,
                  row: dict[str, Any], change: str) -> None:
    await conn.execute(
        "INSERT INTO app.requirement_changes (task_flow_id, id, change, position, category, "
        "title, description, priority, rationale, component, updated_at) VALUES "
        "(%(flow)s, %(id)s, %(change)s, %(position)s, %(category)s, %(title)s, "
        "%(description)s, %(priority)s, %(rationale)s, %(component)s, now()) "
        "ON CONFLICT (task_flow_id, id) DO UPDATE SET change = EXCLUDED.change, "
        "position = EXCLUDED.position, category = EXCLUDED.category, title = EXCLUDED.title, "
        "description = EXCLUDED.description, priority = EXCLUDED.priority, "
        "rationale = EXCLUDED.rationale, component = EXCLUDED.component, updated_at = now()",
        {**row, "flow": task_flow_id, "change": change})


async def write_session(task_flow_id: str, recorded: Iterable[Mapping[str, Any]],
                        retired: Iterable[Mapping[str, Any]] = ()) -> None:
    """Make the flow's interviewed requirements those `recorded` and `retired` now, as
    each change of the interview happens: recorded, updated, removed, renumbered,
    retired or kept. A recorded requirement without a component key keeps the component
    the flow assigned it before; components assigned to other baseline requirements
    stay."""
    rows: list[dict[str, Any]] = []
    keeps: set[str] = set()
    for index, item in enumerate(recorded):
        row = _row(item, index)
        if row is None:
            continue
        rows.append(row)
        if "component" not in item:
            keeps.add(row["id"])
    ended = [row for index, item in enumerate(retired)
             if (row := _row(item, 10_000 + index)) is not None]
    async with await connect() as conn:
        async with conn.transaction():
            await _ensure_flow(conn, task_flow_id)
            cursor = await conn.execute(
                "SELECT id, change, component FROM app.requirement_changes "
                "WHERE task_flow_id = %s", (task_flow_id,))
            before = {row["id"]: row for row in await cursor.fetchall()}
            cursor = await conn.execute(
                "SELECT b.id, b.component FROM app.requirement_baselines b "
                "JOIN app.requirement_flows f ON f.repository = b.repository "
                "AND f.branch = b.branch WHERE f.task_flow_id = %s", (task_flow_id,))
            base = {row["id"]: row["component"] for row in await cursor.fetchall()}
            current = {row["id"] for row in [*rows, *ended]}
            stale = [requirement_id for requirement_id, row in before.items()
                     if row["change"] in SESSION_CHANGES and requirement_id not in current]
            if stale:
                await conn.execute(
                    "DELETE FROM app.requirement_changes WHERE task_flow_id = %s "
                    "AND id = ANY(%s)", (task_flow_id, stale))
            for row in rows:
                if row["id"] in keeps and before.get(row["id"]):
                    row["component"] = row["component"] or before[row["id"]]["component"]
                await _upsert(conn, task_flow_id, row,
                              CHANGED if row["id"] in base else CREATED)
            for row in ended:
                if row["component"] is None:
                    earlier = before.get(row["id"])
                    row["component"] = (earlier["component"] if earlier
                                        else base.get(row["id"]))
                await _upsert(conn, task_flow_id, row, RETIRED)


async def assign_components(task_flow_id: str,
                            assignments: Mapping[str, str | None]) -> int:
    """Set the component of each requirement of the flow named in `assignments`: on the
    flow's own row, or on a row assigning it to a baseline requirement the flow did not
    change; never on the baseline. Returns how many were assigned."""
    count = 0
    async with await connect() as conn:
        async with conn.transaction():
            await _ensure_flow(conn, task_flow_id)
            for raw_id, component in assignments.items():
                requirement_id = canonical_id(raw_id)
                if requirement_id is None:
                    continue
                cursor = await conn.execute(
                    "UPDATE app.requirement_changes SET component = %s, updated_at = now() "
                    "WHERE task_flow_id = %s AND id = %s AND change <> %s",
                    (component, task_flow_id, requirement_id, REMOVED))
                if cursor.rowcount:
                    count += 1
                    continue
                cursor = await conn.execute(
                    "INSERT INTO app.requirement_changes (task_flow_id, id, change, position, "
                    "category, title, description, priority, rationale, component) "
                    "SELECT f.task_flow_id, b.id, %s, b.position, b.category, b.title, "
                    "b.description, b.priority, b.rationale, %s "
                    "FROM app.requirement_baselines b JOIN app.requirement_flows f "
                    "ON f.repository = b.repository AND f.branch = b.branch "
                    "WHERE f.task_flow_id = %s AND b.id = %s "
                    "ON CONFLICT (task_flow_id, id) DO NOTHING",
                    (ASSIGNED, component, task_flow_id, requirement_id))
                count += cursor.rowcount or 0
    return count


async def apply_mapping(task_flow_id: str, renumbered: Mapping[str, str],
                        removed: Iterable[str] = ()) -> None:
    """Apply what resolving merge conflicts found: each of the flow's requirements in
    `renumbered` now has the ID it maps to, and those `removed` are gone."""
    async with await connect() as conn:
        async with conn.transaction():
            await _ensure_flow(conn, task_flow_id)
            base = await _baseline_ids(conn, task_flow_id)
            for old, new in renumbered.items():
                old_id, new_id = canonical_id(old), canonical_id(new)
                if old_id is None or new_id is None or old_id == new_id:
                    continue
                await conn.execute(
                    "DELETE FROM app.requirement_changes WHERE task_flow_id = %s AND id = %s",
                    (task_flow_id, new_id))
                await conn.execute(
                    "UPDATE app.requirement_changes SET id = %s, change = CASE WHEN "
                    "change IN (%s, %s) THEN %s ELSE change END, updated_at = now() "
                    "WHERE task_flow_id = %s AND id = %s",
                    (new_id, CREATED, CHANGED, CHANGED if new_id in base else CREATED,
                     task_flow_id, old_id))
            for raw_id in removed:
                requirement_id = canonical_id(raw_id)
                if requirement_id is None:
                    continue
                await conn.execute(
                    "DELETE FROM app.requirement_changes WHERE task_flow_id = %s AND id = %s",
                    (task_flow_id, requirement_id))
                if requirement_id in base:
                    await conn.execute(
                        "INSERT INTO app.requirement_changes (task_flow_id, id, change, "
                        "category, title, description, priority) "
                        "VALUES (%s, %s, %s, 'functional', '', '', 'must')",
                        (task_flow_id, requirement_id, REMOVED))


async def copy_flow(source_flow_id: str | None, target_flow_id: str,
                    repository: str | None = None, branch: str | None = None) -> None:
    """Start the layer of a fork's new task flow: linked to the same baseline as the
    source flow (or the given repository and branch), with a copy of the requirements the
    source flow created or changed."""
    async with await connect() as conn:
        async with conn.transaction():
            if source_flow_id:
                cursor = await conn.execute(
                    "SELECT repository, branch FROM app.requirement_flows "
                    "WHERE task_flow_id = %s", (source_flow_id,))
                row = await cursor.fetchone()
                if row is not None and row["repository"] is not None:
                    repository, branch = row["repository"], row["branch"]
            await conn.execute(
                "INSERT INTO app.requirement_flows (task_flow_id, repository, branch) "
                "VALUES (%s, %s, %s) ON CONFLICT (task_flow_id) DO UPDATE SET "
                "repository = COALESCE(EXCLUDED.repository, app.requirement_flows.repository), "
                "branch = COALESCE(EXCLUDED.branch, app.requirement_flows.branch)",
                (target_flow_id, repository, branch))
            if source_flow_id:
                await conn.execute(
                    "INSERT INTO app.requirement_changes (task_flow_id, id, change, position, "
                    "category, title, description, priority, rationale, component) "
                    "SELECT %s, id, change, position, category, title, description, priority, "
                    "rationale, component FROM app.requirement_changes "
                    "WHERE task_flow_id = %s ON CONFLICT (task_flow_id, id) DO NOTHING",
                    (target_flow_id, source_flow_id))


async def fork_flow(source_flow_id: str | None, target_flow_id: str | None,
                    repository: Mapping[str, Any] | None,
                    recorded: Iterable[Mapping[str, Any]] | None = None,
                    retired: Iterable[Mapping[str, Any]] = ()) -> None:
    """Give a fork's new task flow the requirements of the session it was forked from:
    linked to the same branch baseline, with a copy of the source flow's own changes,
    replaced by the forked session's requirements at the fork point (`recorded` and
    `retired`) when given. A store that cannot be written to does not fail the fork:
    the forked session writes its requirements again with its first change."""
    if not target_flow_id:
        return
    try:
        await copy_flow(source_flow_id, target_flow_id,
                        repository.get("full_name") if repository else None,
                        repository.get("branch") if repository else None)
        if recorded is not None:
            await write_session(target_flow_id, list(recorded), list(retired))
    except (OSError, psycopg.Error) as exc:
        logger.warning("Requirements of the fork %s not copied: %s", target_flow_id, exc)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def read(task_flow_id: str, *, component: str | None = None,
               requirement_id: str | None = None, search: str | None = None,
               own_only: bool = False, include_retired: bool = True,
               require_baseline: bool = False,
               ) -> list[StoredRequirement] | None:
    """The task flow's effective requirements: its baseline overlaid by its own layer,
    never another flow's. Filtered by `component` (a name, or 'none' for those without
    one), `requirement_id` and `search` (free text over ID, title and description, every
    word matching); `own_only` keeps only those the flow created or changed. None for a
    task flow the store does not know, and with `require_baseline` for one not linked to
    a branch's baseline."""
    conditions: list[str] = []
    params: dict[str, Any] = {"flow": task_flow_id}
    if component and component.strip():
        if component.strip().lower() in UNASSIGNED:
            conditions.append("r.component IS NULL")
        else:
            conditions.append("lower(r.component) = lower(%(component)s)")
            params["component"] = component.strip()
    if requirement_id and requirement_id.strip():
        conditions.append("r.id = %(id)s")
        params["id"] = canonical_id(requirement_id) or requirement_id.strip()
    for index, term in enumerate((search or "").split()[:10]):
        key = f"term{index}"
        conditions.append(f"(r.id || ' ' || r.title || ' ' || r.description "
                          f"|| ' ' || coalesce(r.component, '')) ILIKE %({key})s")
        params[key] = _like(term)
    if own_only:
        conditions.append(f"r.origin IN ('{CREATED}', '{CHANGED}')")
    if not include_retired:
        conditions.append(f"r.origin <> '{RETIRED}'")
    where = " AND ".join(conditions) or "TRUE"
    query = f"""
        SELECT r.* FROM (
            SELECT c.id, c.category, c.title, c.description, c.priority, c.rationale,
                   c.component, c.change AS origin, c.position, 1 AS layer
            FROM app.requirement_changes c
            WHERE c.task_flow_id = %(flow)s AND c.change <> '{REMOVED}'
            UNION ALL
            SELECT b.id, b.category, b.title, b.description, b.priority, b.rationale,
                   b.component, '{BASELINE}' AS origin, b.position, 0 AS layer
            FROM app.requirement_baselines b
            JOIN app.requirement_flows f
                ON f.repository = b.repository AND f.branch = b.branch
            WHERE f.task_flow_id = %(flow)s AND NOT EXISTS (
                SELECT 1 FROM app.requirement_changes c
                WHERE c.task_flow_id = %(flow)s AND c.id = b.id)
        ) r WHERE {where}
    """
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT repository FROM app.requirement_flows WHERE task_flow_id = %s",
            (task_flow_id,))
        flow = await cursor.fetchone()
        if flow is None or (require_baseline and flow["repository"] is None):
            return None
        cursor = await conn.execute(query, params)  # type: ignore[arg-type]
        rows = await cursor.fetchall()
    rows.sort(key=lambda row: (row["origin"] == RETIRED, _order(row["id"])))
    return [_stored(row) for row in rows]


async def components_of(task_flow_id: str) -> dict[str, str | None] | None:
    """The component of each of the flow's effective requirements by ID; None for a
    flow the store does not know."""
    items = await read(task_flow_id)
    if items is None:
        return None
    return {item["id"]: item["component"] for item in items}


async def delete_flows(task_flow_ids: Iterable[str]) -> int:
    """Delete the layers of these task flows; their baselines stay."""
    ids = list(task_flow_ids)
    if not ids:
        return 0
    async with await connect() as conn:
        cursor = await conn.execute(
            "DELETE FROM app.requirement_flows WHERE task_flow_id = ANY(%s)", (ids,))
        return cursor.rowcount or 0


async def flows_linked_before(cutoff: datetime, limit: int = CLEANUP_BATCH) -> list[str]:
    async with await connect() as conn:
        cursor = await conn.execute(
            "SELECT task_flow_id FROM app.requirement_flows WHERE created_at < %s "
            "ORDER BY created_at LIMIT %s", (cutoff, limit))
        return [row["task_flow_id"] for row in await cursor.fetchall()]


async def delete_closed_flows(retention_days: float) -> int:
    """Delete the layers of task flows completed or cancelled longer than
    `retention_days` ago, and of those Temporal no longer knows."""
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    from app.temporal.client import connect_temporal

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    candidates = await flows_linked_before(cutoff)
    if not candidates:
        return 0
    client = await connect_temporal()
    expired = []
    for task_flow_id in candidates:
        try:
            description = await client.get_workflow_handle(task_flow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                expired.append(task_flow_id)
            continue
        if (description.status in (WorkflowExecutionStatus.COMPLETED,
                                   WorkflowExecutionStatus.CANCELED)
                and description.close_time is not None and description.close_time < cutoff):
            expired.append(task_flow_id)
    deleted = await delete_flows(expired)
    if deleted:
        logger.info("Deleted the requirements of %d closed task flows", deleted)
    return deleted


# The task flow each workflow belongs to, once found: it never changes.
_task_flows: dict[str, str] = {}
TASK_WORKFLOW = "TaskWorkflow"
_MAX_DEPTH = 8


async def task_flow_of(client: Any, workflow_id: str,
                       workflow_type: str | None = None) -> str | None:
    """The top-level task flow (TaskWorkflow) the workflow is a stage of, found through
    its parents; None for a workflow outside any task flow or when Temporal cannot say."""
    if workflow_id in _task_flows:
        return _task_flows[workflow_id]
    current, kind = workflow_id, workflow_type
    try:
        for _ in range(_MAX_DEPTH):
            if kind == TASK_WORKFLOW:
                _task_flows[workflow_id] = current
                return current
            description = await client.get_workflow_handle(current).describe()
            if description.workflow_type == TASK_WORKFLOW:
                _task_flows[workflow_id] = current
                return current
            if not description.parent_id:
                return None
            current, kind = description.parent_id, None
    except Exception as exc:  # noqa: BLE001 - the store is then not used for this call
        logger.info("Task flow of %s not found: %s", workflow_id, exc)
    return None


async def activity_task_flow() -> str | None:
    """The task flow of the workflow the running activity works for; None outside an
    activity, without a Temporal client or outside a task flow."""
    from temporalio import activity

    if not activity.in_activity():
        return None
    info = activity.info()
    try:
        client = activity.client()
    except RuntimeError:
        return None
    if not info.workflow_id:
        return None
    return await task_flow_of(client, info.workflow_id, info.workflow_type)
