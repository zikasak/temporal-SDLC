"""What GET /workflows reports of a flow besides its Temporal status, kept by the flow
itself as search attributes: whether it waits for the user, whether its delivery has
unresolved review findings, and, for a stage of a task flow, whether it is still pending
or was skipped. Safe to import from workflow code.

A stage, an onboarding and a delivery each compute their own flags and upsert them as
they change, and report them to the task flow or onboarding they are a child of, which
aggregates its children's flags into its own. A closed flow never waits (FR-110)."""

import asyncio
from typing import Any

from temporalio import workflow
from temporalio.common import TypedSearchAttributes

from app.temporal.execution_fields import (
    FLOW_STATE_ATTRIBUTE,
    HANDED_OFF,
    REPOSITORY_ATTRIBUTE,
    SKIPPED,
    UNRESOLVED_ATTRIBUTE,
    WAITING_ATTRIBUTE,
)

# Marks the workflow code that keeps the flags; runs recorded before it keep none.
FLAGS_PATCH = "flow-flags"
# The flows that aggregate their children's flags, by the prefix of their IDs.
TASK_FLOW_PREFIX = "task-flow-"
AGGREGATING_PREFIXES = (TASK_FLOW_PREFIX, "onboarding-session-")

Flags = tuple[bool | None, bool | None]


def _updates(flags: Flags) -> list[Any]:
    waiting, unresolved = flags
    return [
        WAITING_ATTRIBUTE.value_set(waiting) if waiting is not None
        else WAITING_ATTRIBUTE.value_unset(),
        UNRESOLVED_ATTRIBUTE.value_set(unresolved) if unresolved is not None
        else UNRESOLVED_ATTRIBUTE.value_unset(),
    ]


class FlowFlags:
    """A workflow that reports its flags: it defines `_flags()`, returning whether it waits
    for the user and whether it has unresolved findings (None for a flag that does not
    apply), calls `_init_flags()` from its init and `_keep_flags()` once its run starts,
    and `_close_flags()` before it closes. Its children's flags arrive by the
    `child_flags` signal, kept in `_children_flags`."""

    def _init_flags(self) -> None:
        self._flags_on = False
        self._flags_closed = False
        self._flags_sent: Flags | None = None
        self._children_flags: dict[str, tuple[bool, bool]] = {}

    def _flags(self) -> Flags:
        return None, None

    def _current_flags(self) -> Flags:
        waiting, unresolved = self._flags()
        if self._flags_closed and waiting is not None:
            waiting = False
        return waiting, unresolved

    def _keep_flags(self) -> None:
        """Start keeping the flags, unless the run was recorded before they were kept or
        works in no repository: flows are listed with their repository's fields, and only
        a namespace that registered those has the flags' search attributes too."""
        started_with = workflow.info().typed_search_attributes
        if not any(pair.key.name == REPOSITORY_ATTRIBUTE.name for pair in started_with):
            return
        if not workflow.patched(FLAGS_PATCH):
            return
        self._flags_on = True
        asyncio.create_task(self._watch_flags())

    async def _watch_flags(self) -> None:
        while True:
            await workflow.wait_condition(
                lambda: self._current_flags() != self._flags_sent)
            await self._send_flags()

    async def _send_flags(self) -> None:
        flags = self._current_flags()
        if flags == self._flags_sent:
            return
        self._flags_sent = flags
        workflow.upsert_search_attributes(_updates(flags))
        parent = workflow.info().parent
        if parent is None or not parent.workflow_id.startswith(AGGREGATING_PREFIXES):
            return
        waiting, unresolved = flags
        try:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(
                "child_flags",
                args=[workflow.info().workflow_id, bool(waiting), bool(unresolved)])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed parent needs no flags
            workflow.logger.info("Flags not reported to %s: %s", parent.workflow_id, exc)

    async def _close_flags(self) -> None:
        """The flow closes: it no longer waits for the user."""
        if not self._flags_on:
            return
        self._flags_closed = True
        await self._send_flags()

    def _children_waiting(self) -> bool:
        return any(waiting for waiting, _ in self._children_flags.values())

    @workflow.signal
    def child_flags(self, workflow_id: str, waiting: bool, unresolved: bool) -> None:
        """A child reports whether it waits for the user and has unresolved findings."""
        self._children_flags[workflow_id] = (bool(waiting), bool(unresolved))


class PendingStage:
    """A stage of a task flow created before the stages it depends on have finished: it is
    started with `pending` set and waits for its task flow's `start_stage` signal, which
    carries the state it continues as a new run with, or for `skip_stage`, which closes it
    as skipped."""

    def _init_pending(self) -> None:
        self._start_with: Any = None
        self._skip_requested = False

    @workflow.signal
    def start_stage(self, state: Any) -> None:
        """Start the pending stage from `state`."""
        if self._start_with is None:
            self._start_with = state

    @workflow.signal
    def skip_stage(self) -> None:
        """Close the pending stage as skipped: it will never run."""
        self._skip_requested = True

    async def _wait_pending(self) -> bool:
        """Wait for the start or the skip; continues as a new run from the start's state,
        or returns once the stage was marked skipped."""
        await workflow.wait_condition(
            lambda: self._start_with is not None or self._skip_requested)
        if self._start_with is not None:
            workflow.logger.info("Pending stage started")
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])
            kept = [pair for pair in workflow.info().typed_search_attributes
                    if pair.key.name != FLOW_STATE_ATTRIBUTE.name]
            await workflow.wait_condition(workflow.all_handlers_finished)
            workflow.continue_as_new(self._start_with,
                                     search_attributes=TypedSearchAttributes(kept))
        mark_skipped()
        workflow.logger.info("Pending stage skipped")
        return True


def mark_skipped() -> None:
    """List the current stage as skipped."""
    workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])


class OpenStage:
    """A stage of a task flow that stays open until its task flow closes. Retention only
    deletes closed workflows, so a stage that closed as soon as it handed the task on
    would be deleted while the task's later stages still run, and its session could no
    longer be read. Instead the stage hands its result to the task flow by the
    `stage_result` signal and waits, listed as completed, for `release_stage`, which the
    task flow sends as it closes; the stage then closes with the same result."""

    def _init_open_stage(self) -> None:
        self._released = False

    @workflow.signal
    def release_stage(self) -> None:
        """The task flow closes: so may this stage."""
        self._released = True

    async def _stay_open(self, result: Any) -> Any:
        """Hand `result` to the task flow and stay open until it releases the stage;
        returns `result`. A stage whose parent is no task flow, or that cannot reach it,
        returns at once."""
        parent = workflow.info().parent
        if parent is None or not parent.workflow_id.startswith(TASK_FLOW_PREFIX):
            return result
        try:
            await workflow.get_external_workflow_handle(parent.workflow_id).signal(
                "stage_result", args=[workflow.info().workflow_id, result])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed task flow takes no result
            workflow.logger.warning("Result not handed to %s: %s", parent.workflow_id, exc)
            return result
        state = workflow.info().typed_search_attributes.get(FLOW_STATE_ATTRIBUTE)
        if state != SKIPPED:
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(HANDED_OFF)])
        workflow.logger.info("Stage handed on, open until its task flow closes")
        await workflow.wait_condition(lambda: self._released)
        await workflow.wait_condition(workflow.all_handlers_finished)
        return result
