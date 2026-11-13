"""What every stage of a task flow, and every onboarding, has in common: a stage created
pending waits for the state it begins with, or closes as skipped; and every such flow
keeps whether it waits for the user and whether its delivery has unresolved review
findings as search attributes, reporting each change to the flow above it. Safe to
import from workflow code."""

import asyncio
from typing import Any

from temporalio import workflow

from app.temporal.execution_fields import (
    FLOW_STATE_ATTRIBUTE, PENDING, SKIPPED, UNRESOLVED_ATTRIBUTE, WAITING_ATTRIBUTE,
)

# The signal a stage sends its task flow, and a delivery its onboarding, with
# (workflow_id, waiting_for_user, unresolved_findings).
STAGE_FLAGS_SIGNAL = "stage_flags"
# Marks the workflow code of onboardings and their deliveries that keeps these flags;
# runs that began before it keep none, and are listed without them.
FLAGS_PATCH = "flow-flags"


class PendingStage:
    """A stage its task flow created before the stages it depends on finished. The
    workflow defines a `begin` signal that stores the state to begin with in `_begun`."""

    def _init_pending(self) -> None:
        self._begun: Any = None
        self._skip_requested = False

    @workflow.signal
    def skip_stage(self) -> None:
        """The stage will never run, such as an architecture after requirements that
        were written because the session was idle: it closes as skipped."""
        self._skip_requested = True

    async def _wait_until_begun(self) -> Any:
        """Wait for the state to begin with; None when the stage was skipped, which it
        then is in listings."""
        await workflow.wait_condition(
            lambda: self._begun is not None or self._skip_requested)
        if self._begun is not None:
            workflow.logger.info("Stage begins")
            return self._begun
        workflow.logger.info("Stage skipped")
        workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED),
                                           WAITING_ATTRIBUTE.value_set(False)])
        return None

    @staticmethod
    def _mark_begun() -> None:
        """A run that began after waiting pending is no longer listed as pending."""
        if workflow.info().typed_search_attributes.get(FLOW_STATE_ATTRIBUTE) == PENDING:
            workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])

    @staticmethod
    def _mark_skipped() -> None:
        """A started stage that ends without ever handing on, such as an architecture
        whose skip the user confirmed."""
        workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_set(SKIPPED)])


class FlowFlags:
    """Keeps the WaitingForUser and UnresolvedFindings search attributes of the workflow
    as its state changes, and reports them to `report_to`. The workflow defines
    `_waiting_for_user()` and `_unresolved_findings()`, None when it has no delivery."""

    def _init_flags(self) -> None:
        self._flags_on = False
        self._flags_sent: tuple[bool, bool | None] | None = None
        self._flags_report_to: str | None = None
        self._flags_watch: asyncio.Task[None] | None = None

    def _waiting_for_user(self) -> bool:
        return False

    def _unresolved_findings(self) -> bool | None:
        return None

    def _current_flags(self) -> tuple[bool, bool | None]:
        return bool(self._waiting_for_user()), self._unresolved_findings()

    def _start_flags(self, report_to: str | None) -> None:
        """Start keeping the flags, from the values the run started with."""
        if self._flags_on:
            return
        self._flags_on = True
        self._flags_report_to = report_to
        attributes = workflow.info().typed_search_attributes
        waiting = attributes.get(WAITING_ATTRIBUTE)
        # A run started without the flags publishes them at once.
        self._flags_sent = (None if waiting is None
                            else (bool(waiting), attributes.get(UNRESOLVED_ATTRIBUTE)))
        self._flags_watch = asyncio.create_task(self._watch_flags())

    async def _watch_flags(self) -> None:
        while True:
            await workflow.wait_condition(lambda: self._current_flags() != self._flags_sent)
            flags = self._current_flags()
            self._publish_flags(flags)
            if self._flags_report_to:
                try:
                    await workflow.get_external_workflow_handle(
                        self._flags_report_to).signal(
                            STAGE_FLAGS_SIGNAL,
                            args=[workflow.info().workflow_id, flags[0], flags[1]])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - its flow may have closed
                    workflow.logger.warning("Flags not reported to %s: %s",
                                            self._flags_report_to, exc)

    def _publish_flags(self, flags: tuple[bool, bool | None]) -> None:
        sent = self._flags_sent
        updates = []
        if sent is None or flags[0] != sent[0]:
            updates.append(WAITING_ATTRIBUTE.value_set(flags[0]))
        if flags[1] is not None and (sent is None or flags[1] != sent[1]):
            updates.append(UNRESOLVED_ATTRIBUTE.value_set(flags[1]))
        if updates:
            workflow.upsert_search_attributes(updates)
        self._flags_sent = flags

    def _close_flags(self) -> None:
        """A closed flow never waits for the user; its unresolved findings stay."""
        if not self._flags_on:
            return
        if self._flags_watch is not None:
            self._flags_watch.cancel()
            self._flags_watch = None
        flags = self._current_flags()
        self._publish_flags((False, flags[1]))
        self._flags_on = False
