"""Prepare the namespace for executions labelled with their repository, branch and
onboarding (see app.temporal.labels); run by the main worker before it starts."""

import logging

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest, ListSearchAttributesRequest,
)
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from app.db import connect
from app.temporal.labels import SEARCH_ATTRIBUTE_KEYS
from app.temporal.registry import ensure_schema as ensure_registry_schema

logger = logging.getLogger(__name__)

LABELS_MIGRATION = "workflow-labels"


async def ensure_search_attributes(client: Client) -> None:
    """Register the label search attributes in the namespace when they are missing."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    missing = {
        key.name: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD
        for key in SEARCH_ATTRIBUTE_KEYS if key.name not in listed.custom_attributes
    }
    if not missing:
        return
    try:
        await client.operator_service.add_search_attributes(AddSearchAttributesRequest(
            namespace=client.namespace, search_attributes=missing))
    except RPCError as exc:
        # Another worker registered them first.
        if exc.status != RPCStatusCode.ALREADY_EXISTS:
            raise
    logger.info("Registered search attributes %s", ", ".join(sorted(missing)))


async def delete_unlabelled_executions(client: Client) -> None:
    """Once per deployment of the labels, delete every execution started before them, which
    has no recorded repository, branch or onboarding, together with its registry record.
    The cutoff is recorded first, so an interrupted cleanup resumes with the same one."""
    async with await connect() as conn:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app.migrations (
                name text PRIMARY KEY,
                cutoff timestamptz NOT NULL DEFAULT now(),
                done boolean NOT NULL DEFAULT false
            )
        """)
        await conn.execute(
            "INSERT INTO app.migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (LABELS_MIGRATION,))
        cursor = await conn.execute(
            "SELECT cutoff, done FROM app.migrations WHERE name = %s", (LABELS_MIGRATION,))
        row = await cursor.fetchone()
    if row is None or row["done"]:
        return
    cutoff = row["cutoff"]
    query = f"StartTime < '{cutoff.isoformat()}'"
    executions = [(item.id, item.run_id) async for item in client.list_workflows(query)]
    for workflow_id, run_id in executions:
        try:
            await client.workflow_service.delete_workflow_execution(
                DeleteWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecution(workflow_id=workflow_id, run_id=run_id),
                ))
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
    async with await connect() as conn:
        await conn.execute(
            "DELETE FROM app.workflow_registry WHERE created_at < %s", (cutoff,))
        await conn.execute(
            "UPDATE app.migrations SET done = true WHERE name = %s", (LABELS_MIGRATION,))
    logger.info("Deleted %d executions started before %s", len(executions), cutoff)


async def prepare_namespace(client: Client) -> None:
    await ensure_search_attributes(client)
    await ensure_registry_schema()
    await delete_unlabelled_executions(client)
