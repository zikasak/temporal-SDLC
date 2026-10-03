"""Deterministic execution policy for single-stage coding workflows."""
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


async def execute_agent_activity(
    activity_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    session_id: str, context: dict[str, Any],
) -> dict[str, Any]:
    return await workflow.execute_activity(
        activity_fn, args=[session_id, context],
        start_to_close_timeout=timedelta(minutes=100),
        heartbeat_timeout=timedelta(minutes=2),
        retry_policy=RetryPolicy(maximum_attempts=1),
    )
