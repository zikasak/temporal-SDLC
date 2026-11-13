"""What a flow reports about itself in GET /workflows while it runs: whether it waits for
the user and whether its delivery has unresolved review findings, and the status of a
stage of a task flow that is pending or was skipped. Each workflow keeps them in its memo
(app.temporal.execution_fields) as they change, and a stage tells its task flow, or a
delivery its onboarding, so that the parent reports them for its whole tree. Safe to
import from workflow code."""

import asyncio
from typing import Any

from temporalio import workflow

from app.temporal.execution_fields import (
    FINDINGS_MEMO, FLOW_STATUS_MEMO, PENDING, SKIPPED, WAITING_MEMO,
)

# Marks the workflow code that keeps these fields; runs from before it report none, and
# are listed with null fields.
FLAGS_PATCH = "flow-flags"
# The signal a stage sends its parent when its fields change: (workflow ID, fields).
CHILD_FLAGS_SIGNAL = "child_flags"
# The parents that gather their children's fields, by their workflow ID's prefix.
GATHERING_PARENTS = ("task-flow-", "onboarding-session-")


class FlowFlags:
    """A workflow that reports its fields: it defines `_flags()`, and calls `_start_flags()`
    at the start of its run."""

    _flags_on = False

    def _flags(self) -> dict[str, Any]:
        """The fields as they are now: WAITING_MEMO and, for a flow with a delivery,
        FINDINGS_MEMO."""
        raise NotImplementedError

    def _start_flags(self) -> None:
        self._flags_on = workflow.patched(FLAGS_PATCH)
        if not self._flags_on:
            return
        if workflow.memo_value(FLOW_STATUS_MEMO, None) == PENDING:
            # A stage that has begun is no longer pending.
            workflow.upsert_memo({FLOW_STATUS_MEMO: None})
        self._flags_task = asyncio.create_task(self._publish_flags())

    async def _publish_flags(self) -> None:
        published: dict[str, Any] | None = None
        while True:
            current = self._flags()
            if current != published:
                workflow.upsert_memo(current)
                published = current
                await notify_parent(current)
            await workflow.wait_condition(lambda: self._flags() != published)

    def _mark_skipped(self) -> None:
        """List the flow as skipped once it has closed: a stage that will never run."""
        if self._flags_on:
            workflow.upsert_memo({FLOW_STATUS_MEMO: SKIPPED, WAITING_MEMO: False})


async def notify_parent(fields: dict[str, Any]) -> None:
    """Tell a parent that gathers its children's fields what this flow's are now."""
    parent = workflow.info().parent
    if parent is None or not parent.workflow_id.startswith(GATHERING_PARENTS):
        return
    try:
        await workflow.get_external_workflow_handle(parent.workflow_id).signal(
            CHILD_FLAGS_SIGNAL, args=[workflow.info().workflow_id, fields])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a closed parent reports nothing any more
        workflow.logger.info("Fields not sent to %s: %s", parent.workflow_id, exc)


class PendingStage:
    """A stage of a task flow created before the stages it depends on have finished: its
    run waits until the task flow begins it with its state, and then continues as a new
    run from that state, or skips it, and then ends as skipped. The workflow defines the
    `begin_stage` signal, typed with its state, which calls `_begin`."""

    def _init_pending(self) -> None:
        self._begun: Any = None
        self._skip_requested = False

    def _begin(self, state: Any) -> None:
        if self._begun is None and not self._skip_requested:
            self._begun = state

    @workflow.signal
    def skip_stage(self) -> None:
        """The task flow will never run this stage."""
        if self._begun is None:
            self._skip_requested = True

    async def _wait_to_begin(self) -> None:
        """Wait while pending; continues as new once begun, returns once skipped."""
        await workflow.wait_condition(
            lambda: self._begun is not None or self._skip_requested)
        if self._begun is None:
            workflow.logger.info("Stage skipped")
            if workflow.patched(FLAGS_PATCH):
                workflow.upsert_memo({FLOW_STATUS_MEMO: SKIPPED, WAITING_MEMO: False})
            return
        workflow.logger.info("Stage begun")
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(self._begun)
