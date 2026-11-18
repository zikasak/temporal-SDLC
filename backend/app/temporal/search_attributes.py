"""Registering the custom search attributes executions are started with, and starting
executions on a namespace that may not have them yet."""

import logging
from typing import Any

from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest,
    ListSearchAttributesRequest,
)
from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode

from app.temporal.execution_fields import SEARCH_ATTRIBUTES

logger = logging.getLogger(__name__)


async def ensure_search_attributes(client: Client) -> None:
    """Register the custom search attributes the executions are started with."""
    listed = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace))
    # Each with its own type: keywords, the keyword list of linked task flows and the
    # booleans of the waiting highlight and the findings marker.
    missing = {key.name: IndexedValueType.ValueType(int(key.indexed_value_type))
               for key in SEARCH_ATTRIBUTES if key.name not in listed.custom_attributes}
    if missing:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(namespace=client.namespace, search_attributes=missing))
        logger.info("Registered search attributes %s", ", ".join(missing))


def _unregistered_attribute(exc: RPCError) -> bool:
    """Whether the start was refused because a search attribute is not registered."""
    return (exc.status == RPCStatusCode.INVALID_ARGUMENT
            and "search attribute" in str(exc.message).lower())


async def start_workflow(client: Client, *args: Any, **kwargs: Any) -> WorkflowHandle:
    """client.start_workflow; when the namespace lacks a search attribute the execution
    is started with, such as one whose worker has not prepared it yet, register the
    attributes and start once more."""
    try:
        return await client.start_workflow(*args, **kwargs)
    except RPCError as exc:
        if not _unregistered_attribute(exc):
            raise
        logger.warning("Search attributes missing (%s); registering them", exc.message)
    await ensure_search_attributes(client)
    return await client.start_workflow(*args, **kwargs)
