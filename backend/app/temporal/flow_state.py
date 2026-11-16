"""What a flow reports about itself while it runs, and how a stage of a task flow waits for
its turn. Safe to import from workflow code.

Every stage of a task flow, every onboarding started since and their deliveries keep two
search attributes up to date: WaitingForUser (unanswered interviewer questions, a coding
agent's questions, open review findings, a skip proposal or a contract decision) and
UnresolvedFindings (open review findings of the flow's delivery that were not accepted).
A child also signals both to its parent (`child_state`), so that the task flow or the
onboarding can show what its children wait for. A closed flow never waits.

A stage of a task flow is started by the task flow as soon as it is known to be needed,
as pending, and waits for the task flow to `begin` it with its real start, or to skip it.
Once the task flow has its delivery's outcome it tells every stage to close the same way
(`close_flow`). A stage that a forked or fix-round task flow links to tells it when it
closes (`linked_closed`)."""

import asyncio
from typing import Any

from temporalio import workflow

from app.temporal.closing import end_cancelled
from app.temporal.execution_fields import (
    FLOW_STATE_ATTRIBUTE, SKIPPED, UNRESOLVED_ATTRIBUTE, WAITING_ATTRIBUTE,
)

FLOW_CANCELLED = "The flow's pull request was closed without being merged."
FLOW_ABANDONED = "The flow ended without a delivery."


async def signal_workflow(workflow_id: str | None, name: str, *args: Any) -> bool:
    """Signal another workflow; false when it cannot be, such as one that has closed."""
    if not workflow_id:
        return False
    try:
        await workflow.get_external_workflow_handle(workflow_id).signal(name, args=list(args))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a closed or missing workflow takes nothing
        workflow.logger.warning("Signal %s to %s not delivered: %s", name, workflow_id, exc)
        return False
    return True


class FlowReporting:
    """Keeps WaitingForUser and UnresolvedFindings of a tracked workflow as it changes.

    The workflow defines `_tracked()`, whether it reports at all (executions from before
    the attributes existed never do), `_waiting()`, `_unresolved()` (None for a flow
    without a delivery) and `_report_to()`, the workflow the reports are signalled to."""

    def _init_reporting(self) -> None:
        self._reported: tuple[bool, bool | None] | None = None
        self._reporter: asyncio.Task[None] | None = None

    def _tracked(self) -> bool:
        return False

    def _waiting(self) -> bool:
        return False

    def _unresolved(self) -> bool | None:
        return None

    def _report_to(self) -> str | None:
        return None

    def _current(self) -> tuple[bool, bool | None]:
        return self._waiting(), self._unresolved()

    def _start_reporting(self) -> None:
        if self._tracked() and self._reporter is None:
            self._reporter = asyncio.create_task(self._report_changes())

    async def _report_changes(self) -> None:
        while True:
            await workflow.wait_condition(lambda: self._current() != self._reported)
            await self._report(self._current())

    async def _report(self, current: tuple[bool, bool | None]) -> None:
        self._reported = current
        waiting, unresolved = current
        updates = [WAITING_ATTRIBUTE.value_set(waiting)]
        if unresolved is not None:
            updates.append(UNRESOLVED_ATTRIBUTE.value_set(unresolved))
        workflow.upsert_search_attributes(updates)
        await signal_workflow(self._report_to(), "child_state",
                              workflow.info().workflow_id, waiting, unresolved)

    async def _stop_reporting(self) -> None:
        """Before the workflow closes: it no longer waits for the user; its findings stay
        as they are."""
        if self._reporter is not None:
            self._reporter.cancel()
            self._reporter = None
        final = (False, self._unresolved())
        if self._tracked() and self._reported != final:
            await self._report(final)


class TaskStage(FlowReporting):
    """A stage of a task flow: requirements, architecture or API design. The workflow
    holds `state` with `task_flow_id`, `pending` and `linked_task_flows`, and defines the
    `begin` signal, which hands it its real start (`_begun`)."""

    def _init_stage(self) -> None:
        self._init_reporting()
        self._begun: Any = None
        self._skip = False
        self._handed: str | None = None
        # How the task flow closed: cancelled (true) or completed (false).
        self._flow_cancelled: bool | None = None

    def _tracked(self) -> bool:
        return bool(self.state.task_flow_id)

    def _report_to(self) -> str | None:
        return self.state.task_flow_id

    @workflow.signal
    def skip_stage(self) -> None:
        """The task flow will never run this pending stage."""
        self._skip = True

    @workflow.signal
    def handed_on(self, session_id: str) -> None:
        """The task flow started the next stage, `session_id`, from this one's hand-off."""
        self._handed = session_id
        self._handed_to(session_id)

    def _handed_to(self, session_id: str) -> None:
        """Record the next stage the task flow started, as the session reports it."""

    @workflow.signal
    def close_flow(self, cancelled: bool) -> None:
        """Every part of the task has ended: the stage closes as the task flow does."""
        self._flow_cancelled = bool(cancelled)

    @workflow.signal
    def link_task_flow(self, flow_id: str) -> None:
        """A forked or fix-round task flow links to this stage, and is told when it
        closes."""
        if flow_id and flow_id not in self.state.linked_task_flows:
            self.state.linked_task_flows.append(flow_id)

    async def _await_begin(self) -> bool:
        """Wait while pending; false when the task flow skipped the stage, which then
        closes as skipped."""
        await workflow.wait_condition(lambda: self._begun is not None or self._skip)
        if self._begun is None:
            workflow.logger.info("Pending stage skipped")
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])
            return False
        state = self._begun
        state.pending = False
        state.task_flow_id = state.task_flow_id or self.state.task_flow_id
        state.linked_task_flows = list(dict.fromkeys(
            [*self.state.linked_task_flows, *state.linked_task_flows]))
        self.state = state
        workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])
        workflow.logger.info("Stage started by its task flow")
        return True

    async def _hand_off(self, signal: str, *args: Any) -> None:
        """Tell the task flow what this stage hands on."""
        if not await signal_workflow(self.state.task_flow_id, signal, *args):
            workflow.logger.warning("The task flow did not take the hand-off %s", signal)

    async def _await_flow_close(self) -> bool:
        """Wait until the task flow closes this stage; true when it was cancelled."""
        await workflow.wait_condition(lambda: self._flow_cancelled is not None)
        return bool(self._flow_cancelled)

    async def _close_stage(self, cancelled: bool, reason: str = FLOW_CANCELLED,
                           skipped: bool = False) -> None:
        """Close the stage: no longer waiting, the task flows linked to it told, and the run
        ended as cancelled when `cancelled`; otherwise the caller returns."""
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._stop_reporting()
        if skipped:
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])
        for flow_id in list(self.state.linked_task_flows):
            await signal_workflow(flow_id, "linked_closed", workflow.info().workflow_id)
        if cancelled:
            await end_cancelled(reason)
