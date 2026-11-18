"""What a flow reports about itself in listings, and how a stage of a task flow waits
until its task flow starts it. Safe to import from workflow code.

A workflow started with the WaitingForUser search attribute (execution_fields with
`flags`) keeps it, and UnresolvedFindings when it was started with that too, up to date
as its state changes; a closed flow never waits for the user, so the attribute is cleared
when the run ends other than by continuing as new. Executions started before these
attributes existed carry neither and change nothing, so their histories replay as they
were recorded.

A stage a task flow creates before the stages it depends on have finished starts as a
placeholder (its state's `pending` is set) marked FlowState=pending: it waits for the task
flow's `begin` signal with its real state and continues as new from it, or for `skip_pending`,
which closes it marked FlowState=skipped."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.temporal.execution_fields import (
        FLOW_STATE_ATTRIBUTE, SKIPPED, UNRESOLVED_ATTRIBUTE, WAITING_ATTRIBUTE,
    )


def _attributes() -> Any:
    return workflow.info().typed_search_attributes


def keeps_flags() -> bool:
    """Whether this run was started with WaitingForUser, and so keeps it up to date."""
    attributes = _attributes()
    return bool(attributes) and attributes.get(WAITING_ATTRIBUTE) is not None


def _keeps_findings() -> bool:
    attributes = _attributes()
    return bool(attributes) and attributes.get(UNRESOLVED_ATTRIBUTE) is not None


def interview_waits(state: Any) -> bool:
    """Whether an interview waits for the user: its interviewer answered last."""
    messages = getattr(state, "messages", None) or []
    return (getattr(state, "status", None) == "interviewing" and bool(messages)
            and messages[-1].get("role") == "assistant")


class FlowFlags:
    """Keeps the run's WaitingForUser and UnresolvedFindings search attributes as
    `_flags()` reports them. `_run_flagged` wraps the body of the workflow's run."""

    def _flags(self) -> tuple[bool, bool]:
        """Whether the flow waits for the user, and whether it has unresolved findings."""
        raise NotImplementedError

    async def _run_flagged(self, body: Callable[[], Awaitable[Any]]) -> Any:
        if not keeps_flags():
            return await body()
        self._flags_ended = False
        self._flags_reported: tuple[bool, bool] | None = None
        watcher = asyncio.create_task(self._watch_flags())
        continued = False
        try:
            return await body()
        except workflow.ContinueAsNewError:
            continued = True
            raise
        finally:
            # Upserts are commands of their own: none may follow the run's last one, so the
            # watcher stops here and the closing value is set before the run ends.
            self._flags_ended = True
            watcher.cancel()
            if not continued:
                self._upsert_flags((False, self._safe_flags()[1]))

    def _safe_flags(self) -> tuple[bool, bool]:
        try:
            waiting, unresolved = self._flags()
        except Exception:  # noqa: BLE001 - a state without the fields waits for nothing
            return False, False
        return bool(waiting), bool(unresolved)

    def _upsert_flags(self, flags: tuple[bool, bool]) -> None:
        if flags == self._flags_reported:
            return
        updates = [WAITING_ATTRIBUTE.value_set(flags[0])]
        if _keeps_findings():
            updates.append(UNRESOLVED_ATTRIBUTE.value_set(flags[1]))
        workflow.upsert_search_attributes(updates)
        self._flags_reported = flags

    async def _watch_flags(self) -> None:
        while not self._flags_ended:
            self._upsert_flags(self._safe_flags())
            reported = self._flags_reported
            await workflow.wait_condition(
                lambda: self._flags_ended or self._safe_flags() != reported)


class PendingStage:
    """A stage created pending by its task flow. The workflow's state has `pending`; its
    `begin` and `skip_pending` signals call `_begin` and `_skip`, and its run starts with
    `if self.state.pending: return await self._wait_pending()`."""

    _begun: Any = None
    _skipped: bool = False

    def _begin(self, state: Any) -> None:
        if self._begun is None and not self._skipped:
            self._begun = state

    def _skip(self) -> None:
        if self._begun is None:
            self._skipped = True

    async def _wait_pending(self) -> None:
        """Wait to be started or skipped. Started, the stage continues as new from the
        state it was given, no longer pending."""
        await workflow.wait_condition(lambda: self._begun is not None or self._skipped)
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self._begun is None:
            workflow.logger.info("Stage skipped")
            workflow.upsert_search_attributes([
                FLOW_STATE_ATTRIBUTE.value_set(SKIPPED),
                *([WAITING_ATTRIBUTE.value_set(False)] if keeps_flags() else [])])
            return None
        workflow.logger.info("Stage started by its task flow")
        workflow.upsert_search_attributes([FLOW_STATE_ATTRIBUTE.value_unset()])
        self._begun.pending = False
        workflow.continue_as_new(self._begun)
