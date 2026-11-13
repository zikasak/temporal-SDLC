import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError, CancelledError, ChildWorkflowError, TerminatedError,
)

from app.temporal.flags import FlowFlags

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.architecture.models import ArchitectureState
    from app.delivery.models import (
        TASK_IDLE_TIMEOUT, DeliveryState, delivery_id, pending_delivery,
    )
    from app.tasks.activities import name_task_flow
    from app.tasks.models import (
        API_DESIGN_PREFIX, ARCHITECTURE_PREFIX, STAGE_TYPES, TaskFlowState, TaskStageRecord,
        as_dict, fallback_name, stage_record, stages_of, with_suffix,
    )
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        DELIVERY_MEMO, FINDINGS_MEMO, NAME_MEMO, WAITING_MEMO, execution_fields,
        pending_memo,
    )

FLOW_CANCELLED = "The task ended without a completed delivery."
# Outcomes of a delivery child stopped from outside or failed, which the user can start
# again (delivery service `_restart`) for as long as TASK_IDLE_TIMEOUT.
RESTARTABLE = ("failed", "terminated")


@workflow.defn
class TaskWorkflow(FlowFlags):
    """The top-level flow of one task. It holds the task's requirements, architecture, API
    design and delivery as its direct children, none of them a child of another: each is
    created as soon as the task is known to need it, waits as pending until the stages
    before it have finished, and is begun by this flow with what the stage before handed
    on; one that will never run is skipped. The flow is named by the LLM from what needs
    to be done, and again from the finalized requirements. It stays open until every
    child has closed, and then closes with its delivery's status: completed when the
    delivery's pull request was merged, cancelled otherwise.

    A fork of a session starts a task flow of its own with the forked session as its
    first child, and one started by a fix round of a closed delivery starts with the fix
    delivery; either links to the earlier stages of the original task."""

    @workflow.init
    def __init__(self, state: TaskFlowState) -> None:
        self.state = state
        self._handles: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[None]] = []
        # The fields each child last reported (app.temporal.flags), by workflow ID.
        self._child_flags: dict[str, dict[str, Any]] = {}
        # The state to start a stopped delivery child again from.
        self._redelivery: DeliveryState | None = None

    # -- reporting ---------------------------------------------------------------------

    @workflow.query
    def get_state(self) -> TaskFlowState:
        return self.state

    @workflow.update
    async def wait_created(self) -> None:
        """Done once the flow has created its first children, so that the session it
        started with can be reached."""
        await workflow.wait_condition(lambda: self.state.created)

    @workflow.signal
    def child_flags(self, workflow_id: str, fields: dict[str, Any]) -> None:
        """A child reports whether it waits for the user and, a delivery, whether it has
        unresolved review findings."""
        self._child_flags[workflow_id] = dict(fields)

    def _flags(self) -> dict[str, Any]:
        running = {item.workflow_id for item in self.state.stages if item.status == "running"}
        waiting = any(bool(fields.get(WAITING_MEMO))
                      for workflow_id, fields in self._child_flags.items()
                      if workflow_id in running)
        delivery = stage_record(self.state, "delivery")
        findings = bool(delivery and self._child_flags.get(delivery.workflow_id, {}).get(
            FINDINGS_MEMO))
        return {WAITING_MEMO: waiting, FINDINGS_MEMO: findings}

    # -- the run -----------------------------------------------------------------------

    @workflow.run
    async def run(self, state: TaskFlowState) -> str | None:
        self._start_flags()
        if not self.state.name:
            self.state.name = fallback_name(self.state.task)
        await self._create_first()
        self.state.created = True
        if not self.state.suffix and self.state.task:
            self._tasks.append(asyncio.create_task(self._rename(self.state.task, [])))
        await workflow.wait_condition(
            lambda: all(task.done() for task in self._tasks)
            and all(item.status in ("closed", "skipped") for item in self.state.stages))
        await workflow.wait_condition(workflow.all_handlers_finished)
        delivery = stage_record(self.state, "delivery")
        self.state.outcome = (delivery.outcome if delivery and delivery.status == "closed"
                              and delivery.outcome == "completed" else "cancelled")
        workflow.logger.info("Every child of the task closed; the task is %s",
                             self.state.outcome)
        if self.state.outcome != "completed":
            await end_cancelled(FLOW_CANCELLED)
        return self.state.outcome

    async def _rename(self, task: str, requirements: list[dict[str, Any]]) -> None:
        try:
            name = await workflow.execute_activity(
                name_task_flow, args=[task, requirements],
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("The task could not be named: %s", exc.cause or exc)
            return
        self.state.name = with_suffix(name, self.state.suffix)
        workflow.upsert_memo({NAME_MEMO: self.state.name})

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{workflow.uuid4().hex}"

    async def _create_first(self) -> None:
        """The first child, with its state, and the later stages of the task as pending."""
        task_id = workflow.info().workflow_id
        order = stages_of(self.state.kind)
        first = self.state.first_stage
        later = order[order.index(first) + 1:] if first in order else ()
        ids: dict[str, str] = {first: self.state.first_session_id}
        for stage in later:
            if stage == "architecture":
                ids[stage] = self._new_id(ARCHITECTURE_PREFIX)
            elif stage == "api_design":
                ids[stage] = self._new_id(API_DESIGN_PREFIX)
        # A delivery is named after the session whose task it builds: the API design, or
        # the requirements of a task that leaves the API as it is.
        delivery_session = (self.state.delivery_session_id
                            or ids.get("api_design") or ids.get("requirements"))
        if first == "requirements" and self.state.requirements is not None:
            self.state.requirements.task_flow_id = task_id
            await self._start("requirements", ids[first], self.state.requirements)
            self.state.requirements_session_id = ids[first]
        elif first == "architecture" and self.state.architecture is not None:
            self.state.architecture.task_flow_id = task_id
            self.state.architecture.next_session_id = ids.get("api_design")
            await self._start("architecture", ids[first], self.state.architecture)
            self.state.architecture_session_id = ids[first]
        elif first == "api_design" and self.state.api_design is not None:
            self.state.api_design.task_flow_id = task_id
            await self._start("api_design", ids[first], self.state.api_design)
            self.state.api_design_session_id = ids[first]
        elif first == "delivery" and self.state.delivery is not None:
            self.state.delivery.spec.task_flow_id = task_id
            await self._start("delivery", delivery_id(delivery_session or ""),
                              self.state.delivery)
        for stage in later:
            if stage == "delivery":
                await self._create_pending("delivery", delivery_id(delivery_session or ""),
                                           delivery_session)
            else:
                await self._create_pending(stage, ids[stage])

    # -- children ----------------------------------------------------------------------

    async def _start(self, stage: str, workflow_id: str, payload: Any,
                     pending: bool = False) -> TaskStageRecord:
        """Create the stage's child, running from `payload` or, pending, waiting for it."""
        record = TaskStageRecord(stage=stage, workflow_id=workflow_id,  # type: ignore[arg-type]
                                 status="pending" if pending else "running")
        self.state.stages.append(record)
        fields = execution_fields(self.state.repository,
                                  memo_fields=pending_memo() if pending else None)
        handle = await workflow.start_child_workflow(
            STAGE_TYPES[stage], payload, id=workflow_id, result_type=dict, **fields)
        self._handles[workflow_id] = handle
        workflow.logger.info("%s child %s %s", stage, workflow_id,
                             "created" if pending else "started")
        if stage == "delivery":
            workflow.upsert_memo({DELIVERY_MEMO: workflow_id})
        else:
            await self._register(workflow_id)
        self._tasks.append(asyncio.create_task(self._follow(record, handle)))
        return record

    async def _create_pending(self, stage: str, workflow_id: str,
                              session_id: str | None = None) -> TaskStageRecord:
        task_id = workflow.info().workflow_id
        payload: Any
        if stage == "architecture":
            payload = ArchitectureState(pending=True, task_flow_id=task_id)
        elif stage == "api_design":
            payload = ApiDesignState(pending=True, task_flow_id=task_id)
        else:
            payload = pending_delivery(session_id or workflow_id, task_id)
        return await self._start(stage, workflow_id, payload, pending=True)

    async def _register(self, session_id: str) -> None:
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # Unrecorded, the session is still listed and served while Temporal is up.
            workflow.logger.warning("Session %s not recorded: %s", session_id,
                                    exc.cause or exc)

    async def _skip(self, record: TaskStageRecord) -> None:
        """Close a pending child that will never run, as skipped."""
        if record.status != "pending":
            return
        record.status = "skipped"
        workflow.logger.info("%s child %s skipped", record.stage, record.workflow_id)
        try:
            await self._handles[record.workflow_id].signal("skip_stage")
        except Exception as exc:  # noqa: BLE001 - a child that closed has nothing to skip
            workflow.logger.warning("Child %s not skipped: %s", record.workflow_id, exc)

    async def _skip_pending(self, keep: set[str] | None = None) -> None:
        for record in list(self.state.stages):
            if record.status == "pending" and record.workflow_id not in (keep or set()):
                await self._skip(record)

    async def _begin(self, stage: str, workflow_id: str, payload: Any) -> TaskStageRecord:
        """Begin the stage's pending child `workflow_id` with `payload`, or, when the task
        did not have it pending (its kind changed in the interview), create it running."""
        record = stage_record(self.state, stage)
        if record is not None and record.status == "pending" and record.workflow_id == workflow_id:
            await self._handles[workflow_id].signal("begin_stage", payload)
            record.status = "running"
            workflow.logger.info("%s child %s begun", stage, workflow_id)
            return record
        if record is not None and record.status == "pending":
            await self._skip(record)
        return await self._start(stage, workflow_id, payload)

    async def _pending_id(self, stage: str, prefix: str) -> str:
        """The ID of the stage's pending child, created when the task has none."""
        record = stage_record(self.state, stage)
        if record is not None and record.status == "pending":
            return record.workflow_id
        return (await self._create_pending(stage, self._new_id(prefix))).workflow_id

    async def _pending_delivery(self, session_id: str) -> None:
        """Have the pending delivery child named after `session_id`, skipping one named
        after another session."""
        workflow_id = delivery_id(session_id)
        record = stage_record(self.state, "delivery")
        if record is not None and record.status == "pending":
            if record.workflow_id == workflow_id:
                return
            await self._skip(record)
        await self._create_pending("delivery", workflow_id, session_id)

    # -- following the children ----------------------------------------------------------

    async def _follow(self, record: TaskStageRecord, handle: Any) -> None:
        result: Any = None
        try:
            result = await handle
        except ChildWorkflowError as exc:
            workflow.logger.warning("%s child %s did not complete: %s", record.stage,
                                    record.workflow_id, exc.cause or exc)
            if isinstance(exc.cause, CancelledError):
                outcome = "cancelled"
            elif isinstance(exc.cause, TerminatedError):
                outcome = "terminated"
            else:
                outcome = "failed"
        else:
            outcome = "completed"
        skipped = record.status == "skipped"
        record.status = "skipped" if skipped else "closed"
        record.outcome = outcome
        flags = self._child_flags.setdefault(record.workflow_id, {})
        flags[WAITING_MEMO] = False
        if skipped:
            return
        if record.stage == "requirements":
            await self._requirements_closed(outcome, as_dict(result))
        elif record.stage == "architecture":
            await self._architecture_closed(outcome, as_dict(result))
        elif record.stage == "api_design":
            await self._design_closed(outcome, as_dict(result))
        elif outcome in RESTARTABLE:
            await self._await_redelivery(record)

    async def _requirements_closed(self, outcome: str, result: dict[str, Any]) -> None:
        handed = as_dict(result.get("next")) if outcome == "completed" else {}
        if outcome == "completed" and result.get("finalized"):
            # The name is written again from the finalized requirements.
            self._tasks.append(asyncio.create_task(
                self._rename(self.state.task, list(result.get("requirements") or []))))
        if result.get("kind"):
            self.state.kind = result["kind"]
        stage = handed.get("stage")
        if stage == "delivery":
            session_id = str(handed["session_id"])
            await self._skip_pending(keep={delivery_id(session_id)})
            await self._pending_delivery(session_id)
            await self._begin("delivery", delivery_id(session_id), handed["delivery"])
        elif stage == "architecture":
            design_id = await self._pending_id("api_design", API_DESIGN_PREFIX)
            await self._pending_delivery(design_id)
            architecture_id = await self._pending_id("architecture", ARCHITECTURE_PREFIX)
            payload = dict(handed["architecture"])
            payload["task_flow_id"] = workflow.info().workflow_id
            payload["next_session_id"] = design_id
            await self._begin("architecture", architecture_id, payload)
            self.state.architecture_session_id = architecture_id
        elif stage == "api_design":
            record = stage_record(self.state, "architecture")
            if record is not None:
                await self._skip(record)
            design_id = await self._pending_id("api_design", API_DESIGN_PREFIX)
            await self._pending_delivery(design_id)
            payload = dict(handed["api_design"])
            payload["task_flow_id"] = workflow.info().workflow_id
            await self._begin("api_design", design_id, payload)
            self.state.api_design_session_id = design_id
        else:
            # Abandoned, failed, or written because the session was idle for a day: no
            # later stage runs.
            await self._skip_pending()

    async def _architecture_closed(self, outcome: str, result: dict[str, Any]) -> None:
        design_id = result.get("api_design_session_id")
        design = result.get("design")
        if outcome != "completed" or not design_id or not isinstance(design, dict):
            await self._skip_pending()
            return
        payload = dict(design)
        payload["task_flow_id"] = workflow.info().workflow_id
        await self._pending_delivery(str(design_id))
        await self._begin("api_design", str(design_id), payload)
        self.state.api_design_session_id = str(design_id)

    async def _design_closed(self, outcome: str, result: dict[str, Any]) -> None:
        handed = as_dict(result.get("next")) if outcome == "completed" else {}
        if handed.get("stage") != "delivery":
            await self._skip_pending()
            return
        session_id = str(handed["session_id"])
        await self._pending_delivery(session_id)
        await self._begin("delivery", delivery_id(session_id), handed["delivery"])

    # -- a delivery started again --------------------------------------------------------

    async def _await_redelivery(self, record: TaskStageRecord) -> None:
        """A delivery that failed or was stopped from outside waits for the user to start
        it again from where it stopped, as the flow's child again."""
        try:
            await workflow.wait_condition(lambda: self._redelivery is not None,
                                          timeout=TASK_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            return
        state, self._redelivery = self._redelivery, None
        if state is None:
            return
        workflow.logger.info("Starting the stopped delivery %s again", record.workflow_id)
        handle = await workflow.start_child_workflow(
            "DeliveryWorkflow", state, id=record.workflow_id, result_type=dict,
            **execution_fields(self.state.repository))
        self._handles[record.workflow_id] = handle
        record.status = "running"
        record.outcome = None
        await self._follow(record, handle)

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start the task's stopped delivery again from `state`."""
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        record = stage_record(self.state, "delivery")
        if (record is None or record.status != "closed" or record.outcome not in RESTARTABLE
                or self._redelivery is not None):
            raise ValueError("This task's delivery cannot be started again.")
