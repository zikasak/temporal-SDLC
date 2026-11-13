import asyncio
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.architecture.models import ArchitectureResult, ArchitectureState
    from app.architecture.source import architecture_state
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryProgress, DeliveryState, delivery_id
    from app.requirements.models import DIRECT_KINDS
    from app.task_flow.activities import name_task_flow, rename_task_flow, running_workflows
    from app.task_flow.models import (
        DESIGNED_KINDS, FINISHED_STAGE_STATUSES, FORK_SUFFIX, STAGE_WORKFLOW_TYPES,
        ApiDesignHandOff, RequirementsHandOff, StageRecord, TaskState, direct_delivery,
        pending_delivery, stage_ids, suffixed,
    )
    from app.task_flow.stage import FlowFlags
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        TASK_NAME_ATTRIBUTE, TASK_STAGES_MEMO, execution_fields,
    )

# How often a task flow whose own stages have all closed checks whether the earlier
# stages of another task it links to have closed too.
LINK_CHECK_INTERVAL = timedelta(minutes=10)
TASK_CANCELLED = "The task ended without a merged delivery."
RESULT_TYPES: dict[str, Any] = {
    "requirements": Optional[RequirementsHandOff],
    "architecture": Optional[ArchitectureResult],
    "api_design": Optional[ApiDesignHandOff],
    "delivery": Optional[DeliveryProgress],
}


@workflow.defn
class TaskWorkflow(FlowFlags):
    """The top-level flow of one task. It creates every stage the task needs as its own
    child at once: requirements, architecture, API design and delivery for a new
    service or an API change, requirements and delivery for a bugfix or a documentation
    change. A stage whose earlier stages have not finished waits pending; the flow
    starts it once they hand on, and closes it as skipped when it will never run. The
    flow keeps the task's short name, and whether any stage waits for the user or its
    delivery has unresolved review findings. It closes once every stage, and every
    earlier stage of another task it links to, has closed: completed when its delivery
    completed (its pull request was merged, or no pull request could be opened after the
    push), cancelled otherwise."""

    @workflow.init
    def __init__(self, state: TaskState) -> None:
        self.state = state
        self._handles: dict[str, workflow.ChildWorkflowHandle] = {}
        self._followers: list[asyncio.Task[None]] = []
        # What each stage last reported: whether it waits for the user, and whether its
        # delivery has unresolved review findings.
        self._child_flags: dict[str, tuple[bool, bool | None]] = {}
        # The state a stopped delivery is started again from, by the user's retry.
        self._redelivery: DeliveryState | None = None
        # Set once the name was rewritten from the finalized requirements, which the
        # name written from the intake no longer replaces.
        self._renamed = False
        self._init_flags()

    @property
    def _id(self) -> str:
        return workflow.info().workflow_id

    # Flags ------------------------------------------------------------------------

    def _waiting_for_user(self) -> bool:
        return any(self._child_flags.get(record.workflow_id, (False, None))[0]
                   for record in self.state.stages if record.status == "running")

    def _unresolved_findings(self) -> bool | None:
        return any(bool(self._child_flags.get(record.workflow_id, (False, None))[1])
                   for record in self.state.stages
                   if record.stage == "delivery" and record.status != "skipped")

    @workflow.signal
    def stage_flags(self, workflow_id: str, waiting: bool, unresolved: bool | None) -> None:
        """A stage reports whether it waits for the user and has open review findings."""
        previous = self._child_flags.get(workflow_id, (False, None))
        self._child_flags[workflow_id] = (
            bool(waiting), previous[1] if unresolved is None else bool(unresolved))

    # Queries and updates -----------------------------------------------------------

    @workflow.query
    def get_state(self) -> TaskState:
        return self.state

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start the task's stopped delivery again from `state`."""
        state.pending = False
        state.task_flow_id = self._id
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        if self._redelivery is not None or not any(
                record.stage == "delivery" and record.status == "stopped"
                for record in self.state.stages):
            raise ValueError("This task's delivery cannot be started again.")

    # The run -----------------------------------------------------------------------

    @workflow.run
    async def run(self, state: TaskState) -> str | None:
        self._start_flags(None)
        for record in list(self.state.stages):
            await self._create(record, self._initial_state(record))
        # The stages' starts are with their workflows now; the query stays small.
        self.state.requirements = None
        self.state.architecture = None
        self.state.api_design = None
        self.state.delivery = None
        if self.state.origin == "new" and self.state.task:
            self._spawn(self._name_from_task())
        await workflow.wait_condition(
            lambda: all(record.status in FINISHED_STAGE_STATUSES
                        for record in self.state.stages))
        await self._wait_for_links()
        await workflow.wait_condition(workflow.all_handlers_finished)
        self._close_flags()
        deliveries = [record for record in self.state.stages
                      if record.stage == "delivery" and record.status != "skipped"]
        if deliveries and deliveries[-1].outcome == "completed":
            self.state.outcome = "completed"
            workflow.logger.info("Task completed with its delivery")
            return self.state.name
        self.state.outcome = "cancelled"
        workflow.logger.info("Task ended without a completed delivery")
        await end_cancelled(TASK_CANCELLED)

    def _spawn(self, coroutine: Any) -> None:
        self._followers.append(asyncio.create_task(coroutine))

    def _record(self, workflow_id: str) -> StageRecord | None:
        return next((record for record in self.state.stages
                     if record.workflow_id == workflow_id), None)

    def _initial_state(self, record: StageRecord) -> Any:
        """What a stage is started with: its start for the stage that runs at once, a
        placeholder for a pending one."""
        repository = self.state.repository
        if record.stage == "requirements":
            return self.state.requirements
        if record.stage == "architecture":
            if record.status != "pending" and self.state.architecture is not None:
                return self.state.architecture
            design = next((item.workflow_id for item in self.state.stages
                           if item.stage == "api_design"), None)
            return ArchitectureState(
                kind="api_change" if self.state.kind == "api_change" else "new_service",
                repository=repository, task_flow_id=self._id, next_session_id=design,
                pending=True)
        if record.stage == "api_design":
            if record.status != "pending" and self.state.api_design is not None:
                return self.state.api_design
            return ApiDesignState(kind=self.state.kind, repository=repository,  # type: ignore[arg-type]
                                  task_flow_id=self._id, pending=True)
        if record.status != "pending" and self.state.delivery is not None:
            return self.state.delivery
        return pending_delivery(record.workflow_id.removeprefix("delivery-"),
                                self.state.kind, repository, self._id)

    async def _create(self, record: StageRecord, state: Any) -> None:
        """Start the stage as this flow's child, pending or running, and follow it."""
        handle = await workflow.start_child_workflow(
            STAGE_WORKFLOW_TYPES[record.stage], state, id=record.workflow_id,
            result_type=RESULT_TYPES[record.stage],
            **execution_fields(self.state.repository, task_flow_id=self._id,
                               pending=record.status == "pending", flags=True,
                               findings=record.stage == "delivery"))
        self._handles[record.workflow_id] = handle
        workflow.logger.info("Stage %s %s created %s", record.stage, record.workflow_id,
                             record.status)
        self._spawn(self._follow(record))

    def _remember_stages(self) -> None:
        workflow.upsert_memo({TASK_STAGES_MEMO: stage_ids(self.state)})

    async def _add(self, stage: str, workflow_id: str, state: Any = None) -> StageRecord:
        """A stage the task turned out to need once its requirements were finalized, such
        as after the interview changed the kind of task: pending, or begun with `state`."""
        record = StageRecord(stage=stage, workflow_id=workflow_id,  # type: ignore[arg-type]
                             status="pending" if state is None else "running")
        self.state.stages.append(record)
        await self._create(record, state if state is not None else self._initial_state(record))
        self._remember_stages()
        return record

    async def _begin(self, stage: str, workflow_id: str, state: Any) -> None:
        """Start the pending stage `workflow_id` with `state`; one the task did not have
        yet starts as a new child."""
        record = self._record(workflow_id)
        if record is None:
            await self._add(stage, workflow_id, state)
        elif record.status == "pending":
            await self._handles[workflow_id].signal("begin", state)
            record.status = "running"
            workflow.logger.info("Stage %s %s started", stage, workflow_id)
        else:
            workflow.logger.warning("Stage %s %s cannot start: it is %s", stage, workflow_id,
                                    record.status)
            return
        if stage != "delivery":
            await self._register(workflow_id)

    async def _skip(self, record: StageRecord) -> None:
        if record.status != "pending":
            return
        record.status = "skipped"
        try:
            await self._handles[record.workflow_id].signal("skip_stage")
        except Exception as exc:  # noqa: BLE001 - a closed stage has nothing to skip
            workflow.logger.warning("Stage %s not told it is skipped: %s",
                                    record.workflow_id, exc)
        workflow.logger.info("Stage %s %s skipped", record.stage, record.workflow_id)

    async def _skip_pending(self, keep: tuple[str, ...] = ()) -> None:
        for record in list(self.state.stages):
            if record.workflow_id not in keep:
                await self._skip(record)

    async def _register(self, session_id: str) -> None:
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            # Unrecorded, the session is still listed and served while Temporal is up.
            workflow.logger.warning("Session %s not recorded: %s", session_id,
                                    exc.cause or exc)

    # Following the stages ---------------------------------------------------------

    async def _follow(self, record: StageRecord) -> None:
        """Wait for the stage to close, then hand on what it finished to the next one."""
        while True:
            handle = self._handles[record.workflow_id]
            outcome = "completed"
            result: Any = None
            try:
                result = await handle
            except ChildWorkflowError as exc:
                outcome = ("cancelled" if isinstance(exc.cause, CancelledError)
                           else "failed")
                workflow.logger.warning("Stage %s did not complete: %s", record.workflow_id,
                                        exc.cause or exc)
            flags = self._child_flags.get(record.workflow_id, (False, None))
            self._child_flags[record.workflow_id] = (False, flags[1])
            if record.status == "skipped":
                return
            if record.stage == "delivery" and outcome == "failed":
                # Stopped from outside: the user can start it again from where it stopped.
                record.status = "stopped"
                if await self._restarted(record):
                    continue
            try:
                if outcome == "completed":
                    await self._hand_off(record, result)
                else:
                    await self._skip_pending()
            finally:
                record.status = "closed"
                record.outcome = outcome
            return

    async def _restarted(self, record: StageRecord) -> bool:
        try:
            await workflow.wait_condition(lambda: self._redelivery is not None,
                                          timeout=TASK_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        state, self._redelivery = self._redelivery, None
        self._handles[record.workflow_id] = await workflow.start_child_workflow(
            "DeliveryWorkflow", state, id=record.workflow_id,
            result_type=RESULT_TYPES["delivery"],
            **execution_fields(self.state.repository, task_flow_id=self._id,
                               flags=True, findings=True))
        record.status = "running"
        workflow.logger.info("Starting the stopped delivery again")
        return True

    async def _hand_off(self, record: StageRecord, result: Any) -> None:
        if record.stage == "requirements":
            if (isinstance(result, RequirementsHandOff) and result.finalized
                    and result.state.status == "completed"):
                self._spawn(self._rename(result))
                await self._after_requirements(record.workflow_id, result)
            else:
                # Written because the session was idle, or not written: nothing follows.
                await self._skip_pending()
        elif record.stage == "architecture":
            if isinstance(result, ArchitectureResult):
                design = result.design
                design.task_flow_id = self._id
                design.pending = False
                await self._begin("api_design", result.api_design_session_id, design)
            else:
                await self._skip_pending()
        elif record.stage == "api_design":
            if isinstance(result, ApiDesignHandOff) and result.delivery is not None:
                delivery = result.delivery
                delivery.task_flow_id = self._id
                delivery.pending = False
                await self._begin("delivery", delivery_id(record.workflow_id), delivery)
            else:
                await self._skip_pending()

    async def _after_requirements(self, session_id: str, hand: RequirementsHandOff) -> None:
        """Start what follows the finalized requirements: the architecture of a new
        service or an API change, the delivery of a bugfix or a documentation change."""
        source = hand.state
        self.state.kind = source.kind
        if source.kind in DESIGNED_KINDS and source.repository:
            architecture = next((item for item in self.state.stages
                                 if item.stage == "architecture"
                                 and item.status == "pending"), None)
            design = next((item for item in self.state.stages
                           if item.stage == "api_design" and item.status == "pending"), None)
            design_id = (design.workflow_id if design
                         else f"api-design-session-{workflow.uuid4().hex}")
            delivery = delivery_id(design_id)
            await self._skip_pending(keep=(
                architecture.workflow_id if architecture else "", design_id, delivery))
            if design is None:
                await self._add("api_design", design_id)
            if self._record(delivery) is None:
                await self._add("delivery", delivery)
            start = architecture_state(session_id, source, hand.requirements_text)
            start.task_flow_id = self._id
            start.next_session_id = design_id
            await self._begin("architecture",
                              architecture.workflow_id if architecture
                              else f"architecture-session-{workflow.uuid4().hex}", start)
        elif source.kind in DIRECT_KINDS:
            delivery = delivery_id(session_id)
            await self._skip_pending(keep=(delivery,))
            state = direct_delivery(session_id, source, hand.requirements_text, self._id)
            if state is None:
                await self._skip_pending()
                return
            await self._begin("delivery", delivery, state)
        else:
            await self._skip_pending()

    async def _wait_for_links(self) -> None:
        """Stay open while an earlier stage of another task this flow links to runs."""
        links = list(self.state.links)
        while links:
            try:
                links = await workflow.execute_activity(
                    running_workflows, links,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=3))
            except ActivityError as exc:
                workflow.logger.warning("Linked stages not checked: %s", exc.cause or exc)
            if links:
                await workflow.sleep(LINK_CHECK_INTERVAL)

    # The task's name ----------------------------------------------------------------

    def _set_name(self, name: str) -> None:
        name = name.strip()
        if not name or name == self.state.name:
            return
        self.state.name = name
        workflow.upsert_search_attributes([TASK_NAME_ATTRIBUTE.value_set(name)])
        workflow.logger.info("Task named %s", name)

    async def _name_from_task(self) -> None:
        try:
            name = await workflow.execute_activity(
                name_task_flow, args=[self.state.task, self.state.kind],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2))
        except ActivityError as exc:
            # The name the flow started with stays.
            workflow.logger.warning("Task not named: %s", exc.cause or exc)
            return
        if not self._renamed:
            self._set_name(name)

    async def _rename(self, hand: RequirementsHandOff) -> None:
        try:
            name = await workflow.execute_activity(
                rename_task_flow,
                args=[self.state.name, hand.state.requirements, hand.state.retired],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=2))
        except ActivityError as exc:
            workflow.logger.warning("Task not renamed: %s", exc.cause or exc)
            return
        if self.state.origin == "fork":
            name = suffixed(name, FORK_SUFFIX)
        self._renamed = True
        self._set_name(name)
