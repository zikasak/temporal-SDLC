"""What GET /workflows shows of a flow beyond Temporal's own fields, kept by the flows
themselves: whether it waits for the user (WaitingForUser), whether its delivery has open
review findings nobody accepted (UnresolvedFindings), and for the stages of a task flow
whether they are pending or skipped (FlowState), which task flows of forks link to them
(LinkedTaskFlows) and how they hand the task on. Safe to import from workflow code.

Executions started before flows kept these carry none of them: the mixins only act for a
flow whose state says it does, so their replays issue no new commands."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.architecture.models import ArchitectureState
    from app.delivery.models import DeliveryState
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        FLOW_STATE_ATTRIBUTE, LINKED_ATTRIBUTE, SKIPPED, UNRESOLVED_ATTRIBUTE,
        WAITING_ATTRIBUTE,
    )

# The signals a task flow takes from its stages.
CHILD_MARKERS_SIGNAL = "child_markers"
STAGE_DONE_SIGNAL = "stage_done"
LINKED_STAGE_CLOSED_SIGNAL = "linked_stage_closed"
# The signals a stage takes from its task flow.
START_STAGE_SIGNAL = "start_stage"
SKIP_STAGE_SIGNAL = "skip_stage"
CLOSE_FLOW_SIGNAL = "close_flow"
LINK_TASK_FLOW_SIGNAL = "link_task_flow"
NEXT_STAGE_SIGNAL = "next_stage"
# How a stage of a task flow ends, as its task flow tells it.
COMPLETED = "completed"
CANCELLED = "cancelled"
FLOW_CANCELLED = "The task's pull request was closed without being merged, or it ended without a delivery."


@dataclass
class ChildMarkers:
    """A child's WaitingForUser and UnresolvedFindings, as it reports them to its parent."""

    workflow_id: str
    waiting: bool
    unresolved: bool | None = None


@dataclass
class StageHandOff:
    """What a stage of a task flow hands on to its task flow when its own work is done:
    the next stage's start, or nothing when it ended without handing the task on (it
    failed, or its document was written because it was idle for a day)."""

    stage: str
    workflow_id: str
    handed: bool
    kind: str | None = None
    # Set by an architecture session whose skip the user confirmed: it closes as skipped.
    skipped: bool = False
    # The task's requirements, which the task flow is renamed from once they are final.
    requirements: list[dict[str, Any]] = field(default_factory=list)
    # The start of the next stage: an architecture session, an API design or a delivery.
    architecture: ArchitectureState | None = None
    design: ApiDesignState | None = None
    delivery: DeliveryState | None = None


class FlowMarkers:
    """Keeps WaitingForUser and UnresolvedFindings of the current workflow up to date and
    reports every change of them to the flow it belongs to. Subclasses say whether they
    keep them (`_markers_enabled`), compute them (`_waiting_for_user`,
    `_unresolved_findings`, None for a flow without a delivery) and name the flow they
    report to (`_marker_target`). `_close_markers` clears WaitingForUser before the flow
    closes, as a closed flow never waits."""

    def _markers_enabled(self) -> bool:
        return False

    def _waiting_for_user(self) -> bool:
        return False

    def _unresolved_findings(self) -> bool | None:
        return None

    def _marker_target(self) -> str | None:
        parent = workflow.info().parent
        return parent.workflow_id if parent else None

    def _markers(self) -> tuple[bool, bool | None]:
        closed = getattr(self, "_markers_closed", False)
        return (not closed and self._waiting_for_user(), self._unresolved_findings())

    def _start_markers(self) -> None:
        """Start following the markers; call once at the start of the run."""
        if not self._markers_enabled() or getattr(self, "_markers_task", None) is not None:
            return
        self._markers_closed = False
        self._markers_sent: tuple[bool, bool | None] | None = None
        self._markers_task = asyncio.create_task(self._follow_markers())

    async def _follow_markers(self) -> None:
        while True:
            current = self._markers()
            if current != self._markers_sent:
                await self._publish_markers(current)
                continue
            await workflow.wait_condition(lambda: self._markers() != self._markers_sent)

    async def _publish_markers(self, current: tuple[bool, bool | None]) -> None:
        self._markers_sent = current
        waiting, unresolved = current
        updates = [WAITING_ATTRIBUTE.value_set(waiting)]
        if unresolved is not None:
            updates.append(UNRESOLVED_ATTRIBUTE.value_set(unresolved))
        workflow.upsert_search_attributes(updates)
        target = self._marker_target()
        if not target:
            return
        try:
            await workflow.get_external_workflow_handle(target).signal(
                CHILD_MARKERS_SIGNAL,
                ChildMarkers(workflow.info().workflow_id, waiting, unresolved))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed parent needs no report
            workflow.logger.info("Markers not reported to %s: %s", target, exc)

    async def _close_markers(self) -> None:
        """Clear WaitingForUser, and report it, before the flow closes."""
        if not self._markers_enabled() or getattr(self, "_markers_task", None) is None:
            return
        self._markers_closed = True
        current = self._markers()
        if current != self._markers_sent:
            await self._publish_markers(current)


class TaskStage(FlowMarkers):
    """A requirements, architecture or API design session that is a stage of a task flow
    (`state.task_flow_id`): it reports its markers and its hand-off to the task flow,
    and once its work is done stays open until the task flow closes it with the task's
    outcome. Task flows of forks that link to it are told when it closes."""

    def _init_stage(self) -> None:
        self._close_status: str | None = None

    def _in_task(self) -> bool:
        return bool(getattr(self.state, "task_flow_id", None))  # type: ignore[attr-defined]

    def _markers_enabled(self) -> bool:
        return self._in_task()

    def _marker_target(self) -> str | None:
        return self.state.task_flow_id  # type: ignore[attr-defined]

    @workflow.signal(name=CLOSE_FLOW_SIGNAL)
    def close_flow(self, status: str) -> None:
        """The task flow closes this stage, as the task ended: completed or cancelled."""
        if getattr(self, "_close_status", None) is None:
            self._close_status = status if status in (COMPLETED, CANCELLED) else CANCELLED

    @workflow.signal(name=LINK_TASK_FLOW_SIGNAL)
    def link_task_flow(self, task_flow_id: str) -> None:
        """A fork's task flow links to this stage as an earlier stage of its own."""
        linked = self.state.linked_task_flows  # type: ignore[attr-defined]
        if not task_flow_id or task_flow_id in linked:
            return
        linked.append(task_flow_id)
        workflow.upsert_search_attributes([LINKED_ATTRIBUTE.value_set(list(linked))])

    async def _hand_off(self, handoff: StageHandOff) -> None:
        try:
            await workflow.get_external_workflow_handle(
                self.state.task_flow_id).signal(STAGE_DONE_SIGNAL, handoff)  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed task flow closes the stage
            workflow.logger.warning("Task flow not told of the hand-off: %s", exc)
            if getattr(self, "_close_status", None) is None:
                self._close_status = CANCELLED

    async def _stay_until_closed(self) -> str:
        """Wait for the task flow to close this stage; the status it ends with."""
        await workflow.wait_condition(lambda: self._close_status is not None)
        return self._close_status or CANCELLED

    async def _close_stage(self, status: str) -> None:
        """End this stage: completed, cancelled (raises) or skipped."""
        await self._close_markers()
        if status == SKIPPED:
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])
        await workflow.wait_condition(workflow.all_handlers_finished)
        await tell_linked_flows(self.state.linked_task_flows)  # type: ignore[attr-defined]
        if status == CANCELLED:
            await end_cancelled(FLOW_CANCELLED)


async def tell_linked_flows(linked: list[str]) -> None:
    """Tell the task flows that link to the closing stage that it has closed."""
    for task_flow_id in list(linked):
        try:
            await workflow.get_external_workflow_handle(task_flow_id).signal(
                LINKED_STAGE_CLOSED_SIGNAL, workflow.info().workflow_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed task flow needs no word
            workflow.logger.info("Linked task flow %s not told: %s", task_flow_id, exc)


class PendingStage:
    """A stage a task flow created before it may work: its run waits for the task flow
    to start it with its state, then continues as a new run from that state, or to skip
    it, and then closes with FlowState skipped. Subclasses define the start_stage signal
    with their own state type and call `_started_with`."""

    def _init_pending(self) -> None:
        self._pending_start: Any = None
        self._pending_skipped = False

    def _started_with(self, state: Any) -> None:
        if self._pending_start is None and not self._pending_skipped:
            self._pending_start = state

    @workflow.signal(name=SKIP_STAGE_SIGNAL)
    def skip_stage(self) -> None:
        if self._pending_start is None:
            self._pending_skipped = True

    async def _wait_pending(self) -> None:
        """Wait for the start or the skip: continues as new when started; returns once
        the stage is closed as skipped."""
        await workflow.wait_condition(
            lambda: self._pending_start is not None or self._pending_skipped)
        if self._pending_start is not None:
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])
            await workflow.wait_condition(workflow.all_handlers_finished)
            workflow.logger.info("Stage started by its task flow")
            workflow.continue_as_new(self._pending_start)
        workflow.logger.info("Stage skipped by its task flow")
        workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])
        await workflow.wait_condition(workflow.all_handlers_finished)
