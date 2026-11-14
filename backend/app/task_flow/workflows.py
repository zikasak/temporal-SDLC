import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState, empty_api
    from app.architecture.models import ArchitectureState
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryState, TaskSpec, delivery_id
    from app.requirements.models import DIRECT_KINDS
    from app.task_flow.activities import name_task_flow
    from app.temporal.activities import register_session
    from app.task_flow.models import (
        FORK_SUFFIX, OPEN_CHILD, STAGE_ORDER, STAGE_TYPES, TaskChild, TaskFlowState,
        stages_for,
    )
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        TASK_NAME_ATTRIBUTE, delivery_fields, execution_fields, task_name,
    )
    from app.temporal.flow_markers import (
        CANCELLED, CHILD_MARKERS_SIGNAL, CLOSE_FLOW_SIGNAL, COMPLETED,
        LINK_TASK_FLOW_SIGNAL, LINKED_STAGE_CLOSED_SIGNAL, NEXT_STAGE_SIGNAL,
        SKIP_STAGE_SIGNAL, STAGE_DONE_SIGNAL, START_STAGE_SIGNAL, ChildMarkers,
        FlowMarkers, StageHandOff,
    )

TASK_CANCELLED = "The task's delivery was cancelled or never ran."
NAME_TIMEOUT = timedelta(minutes=3)


@workflow.defn
class TaskWorkflow(FlowMarkers):
    """The top-level flow of one task. It creates its stages as its children up front for
    the kind of task: requirements, architecture, API design and delivery for a new service
    or an API change, requirements and delivery for a bugfix or a documentation change.
    The first stage works at once; each later one is pending until the stage before it
    hands the task on, and is then started, or skipped when the task will never get
    there. The flow keeps the task's short name, written by the LLM, and its children's
    waiting-for-user and unresolved-findings markers, and closes with its delivery's
    status (cancelled when the delivery was skipped) once every child, and every earlier
    stage of another task it links to, has closed."""

    @workflow.init
    def __init__(self, state: TaskFlowState) -> None:
        self.state = state
        self._started = False
        self._handoffs: list[StageHandOff] = []
        self._handles: dict[str, workflow.ChildWorkflowHandle] = {}
        self._followers: list[asyncio.Task[None]] = []
        self._redelivery: DeliveryState | None = None
        self._naming: asyncio.Task[None] | None = None
        # Set when a stopped delivery was not started again in time.
        self._gave_up = False

    # The markers: any open child waiting for the user, and the delivery's open findings.

    def _markers_enabled(self) -> bool:
        return True

    def _marker_target(self) -> str | None:
        return None

    def _waiting_for_user(self) -> bool:
        return any(child.waiting for child in self.state.children
                   if child.status in OPEN_CHILD)

    def _unresolved_findings(self) -> bool | None:
        delivery = self._delivery_child()
        return bool(delivery and delivery.unresolved)

    @workflow.run
    async def run(self, state: TaskFlowState) -> TaskFlowState:
        self._start_markers()
        await self._create_children()
        self._started = True
        await self._link_earlier_stages()
        if self.state.origin is None and self.state.task.strip():
            self._naming = asyncio.create_task(self._rename(self.state.task, []))
        while self.state.outcome is None:
            await workflow.wait_condition(
                lambda: bool(self._handoffs) or self._delivery_over())
            while self._handoffs:
                await self._handle(self._handoffs.pop(0))
            if self._delivery_over():
                self.state.outcome = self._delivery_outcome()
        workflow.logger.info("Task ended %s; closing its stages", self.state.outcome)
        await self._close_stages(self.state.outcome)
        await workflow.wait_condition(
            lambda: all(task.done() for task in self._followers)
            and not self.state.linked_open and workflow.all_handlers_finished())
        await self._close_markers()
        if self.state.outcome == COMPLETED:
            return self.state
        await end_cancelled(TASK_CANCELLED)
        return self.state

    @workflow.query
    def get_state(self) -> TaskFlowState:
        return self.state

    @workflow.update
    async def children_started(self) -> None:
        """Done once the flow has created its children, so that its first stage can be
        served."""
        await workflow.wait_condition(lambda: self._started)

    @workflow.signal(name=STAGE_DONE_SIGNAL)
    def stage_done(self, handoff: StageHandOff) -> None:
        self._handoffs.append(handoff)

    @workflow.signal(name=CHILD_MARKERS_SIGNAL)
    def child_markers(self, markers: ChildMarkers) -> None:
        child = self._child(markers.workflow_id)
        if child is None:
            return
        child.waiting = bool(markers.waiting)
        if markers.unresolved is not None:
            child.unresolved = bool(markers.unresolved)

    @workflow.signal(name=LINKED_STAGE_CLOSED_SIGNAL)
    def linked_stage_closed(self, workflow_id: str) -> None:
        if workflow_id in self.state.linked_open:
            self.state.linked_open.remove(workflow_id)

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this task's stopped delivery again from `state`."""
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        delivery = self._delivery_child()
        if (delivery is None or delivery.status != "stopped" or self._redelivery is not None
                or self.state.outcome is not None):
            raise ValueError("This task's delivery cannot be started again.")

    # Children.

    def _child(self, workflow_id: str) -> TaskChild | None:
        return next((child for child in self.state.children
                     if child.workflow_id == workflow_id), None)

    def _stage_child(self, stage: str) -> TaskChild | None:
        """The child of `stage` that is or was to run: the last one not skipped."""
        children = [child for child in self.state.children if child.stage == stage]
        kept = [child for child in children if child.status != "skipped"]
        return (kept or children or [None])[-1]

    def _delivery_child(self) -> TaskChild | None:
        return self._stage_child("delivery")

    def _delivery_over(self) -> bool:
        """Whether the task's delivery has ended for good: closed, skipped, or stopped and
        not started again while the user could."""
        deliveries = [child for child in self.state.children if child.stage == "delivery"]
        if not deliveries:
            return False
        return all(child.status not in OPEN_CHILD
                   and (child.status != "stopped" or self._gave_up)
                   for child in deliveries)

    def _delivery_outcome(self) -> str:
        delivery = self._delivery_child()
        return COMPLETED if delivery and delivery.status == COMPLETED else CANCELLED

    def _ids(self, stages: tuple[str, ...]) -> dict[str, str]:
        """The workflow IDs of new children of `stages`."""
        ids: dict[str, str] = {}
        for stage in stages:
            if stage == "architecture":
                ids[stage] = f"architecture-session-{workflow.uuid4().hex}"
            elif stage == "api_design":
                ids[stage] = f"api-design-session-{workflow.uuid4().hex}"
        return ids

    def _delivery_session(self) -> str | None:
        """The session whose delivery the task's delivery child is: the API design's, or
        for a task without one its requirements session's."""
        design = self._stage_child("api_design")
        if design is not None and design.status != "skipped":
            return design.workflow_id
        return self.state.requirements_session_id

    async def _create_children(self) -> None:
        """The first stage, working, and the stages after it, pending."""
        first = self.state.first_stage
        stages = [stage for stage in STAGE_ORDER[STAGE_ORDER.index(first):]
                  if stage in stages_for(self.state.kind) or stage == first]
        ids = self._ids(tuple(stages))
        ids[first] = self.state.first_id
        for stage in stages:
            if stage == "delivery" and stage != first:
                design = ids.get("api_design")
                session = design or self.state.requirements_session_id or ""
                ids[stage] = delivery_id(session)
        for stage in stages:
            if stage == first:
                await self._start_first(stage, ids[stage], ids)
            else:
                await self._create_pending(stage, ids[stage])

    async def _start_first(self, stage: str, workflow_id: str, ids: dict[str, str]) -> None:
        task_flow_id = workflow.info().workflow_id
        if stage == "requirements":
            state: Any = self.state.requirements
            state.task_flow_id = task_flow_id
        elif stage == "architecture":
            state = self.state.architecture
            state.task_flow_id = task_flow_id
            state.next_session_id = ids.get("api_design")
            state.pending = False
        elif stage == "api_design":
            state = self.state.design
            state.task_flow_id = task_flow_id
            state.pending = False
        else:
            state = self.state.delivery
            state.spec.task_flow_id = task_flow_id
            state.spec.flow_markers = True
            state.pending = False
        child = TaskChild(stage=stage, workflow_id=workflow_id, status="running")
        await self._start_child(child, state, pending=False)
        # The stage's start is its own state from here on.
        self.state.requirements = None
        self.state.architecture = None
        self.state.design = None
        self.state.delivery = None

    def _placeholder(self, stage: str, workflow_id: str) -> Any:
        task_flow_id = workflow.info().workflow_id
        repository = self.state.repository
        kind = self.state.kind
        if stage == "architecture":
            return ArchitectureState(
                source_session_id=self.state.requirements_session_id or "",
                kind="api_change" if kind == "api_change" else "new_service",
                repository=repository, task_flow_id=task_flow_id, pending=True)
        if stage == "api_design":
            return ApiDesignState(
                source_session_id=self.state.requirements_session_id or "",
                kind=kind, repository=repository, task_flow_id=task_flow_id, pending=True)
        session_id = workflow_id.removeprefix("delivery-")
        return DeliveryState(
            spec=TaskSpec(session_id=session_id, requirements=[], requirements_document="",
                          api=empty_api(), kind=kind, repository=repository,
                          task_flow_id=task_flow_id, flow_markers=True),
            pending=True)

    async def _create_pending(self, stage: str, workflow_id: str) -> None:
        child = TaskChild(stage=stage, workflow_id=workflow_id, status="pending")
        await self._start_child(child, self._placeholder(stage, workflow_id), pending=True)

    async def _start_child(self, child: TaskChild, state: Any, *, pending: bool) -> None:
        task_flow_id = workflow.info().workflow_id
        if child.stage == "delivery":
            fields = delivery_fields(state.spec, pending=pending)
        else:
            fields = execution_fields(self.state.repository, task_flow_id=task_flow_id,
                                      pending=pending)
        handle = await workflow.start_child_workflow(
            STAGE_TYPES[child.stage], state, id=child.workflow_id, **fields)
        workflow.logger.info("%s %s %s", "Created pending" if pending else "Started",
                             child.stage, child.workflow_id)
        self.state.children.append(child)
        if child.stage in ("architecture", "api_design"):
            await self._register(child.workflow_id)
        self._handles[child.workflow_id] = handle
        follow = self._follow_delivery if child.stage == "delivery" else self._follow
        self._followers.append(asyncio.create_task(follow(child, handle)))

    async def _register(self, session_id: str) -> None:
        """Record a session the flow created, as the API records the ones it starts."""
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

    async def _follow(self, child: TaskChild, handle: workflow.ChildWorkflowHandle) -> None:
        try:
            await handle
            status = COMPLETED
        except ChildWorkflowError as exc:
            status = CANCELLED if isinstance(exc.cause, CancelledError) else "stopped"
            workflow.logger.info("Stage %s ended %s: %s", child.workflow_id, status,
                                 exc.cause or exc)
        child.waiting = False
        if child.status == "skipped":
            return
        was_open = child.status in OPEN_CHILD
        child.status = status
        if was_open and not child.handed:
            # It closed without handing the task on: no later stage will run.
            self._handoffs.append(StageHandOff(stage=child.stage,
                                               workflow_id=child.workflow_id, handed=False))

    async def _follow_delivery(self, child: TaskChild,
                               handle: workflow.ChildWorkflowHandle) -> None:
        """Follow the delivery to its end; a delivery stopped from outside, such as a
        terminated one, may be started again (`restart_delivery`) for as long as a
        finished delivery waits for the user."""
        while True:
            try:
                await handle
            except ChildWorkflowError as exc:
                workflow.logger.warning("Delivery did not finish: %s", exc.cause or exc)
                if child.status == "skipped":
                    return
                if isinstance(exc.cause, CancelledError):
                    child.status = CANCELLED
                    child.waiting = False
                    return
            else:
                if child.status != "skipped":
                    child.status = COMPLETED
                child.waiting = False
                return
            child.status = "stopped"
            child.waiting = False
            try:
                await workflow.wait_condition(lambda: self._redelivery is not None,
                                              timeout=TASK_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                self._gave_up = True
                return
            state, self._redelivery = self._redelivery, None
            state.spec.task_flow_id = workflow.info().workflow_id
            child.status = "running"
            workflow.logger.info("Starting the stopped delivery again")
            handle = await workflow.start_child_workflow(
                "DeliveryWorkflow", state, id=child.workflow_id,
                **delivery_fields(state.spec))
            self._handles[child.workflow_id] = handle

    async def _signal(self, workflow_id: str, signal: str, *args: Any) -> bool:
        handle = self._handles.get(workflow_id)
        try:
            if handle is not None:
                await handle.signal(signal, args=list(args))
            else:
                await workflow.get_external_workflow_handle(workflow_id).signal(
                    signal, args=list(args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed stage takes no signal
            workflow.logger.info("Signal %s not sent to %s: %s", signal, workflow_id, exc)
            return False
        return True

    async def _skip(self, child: TaskChild) -> None:
        if child.status != "pending":
            return
        child.status = "skipped"
        child.waiting = False
        await self._signal(child.workflow_id, SKIP_STAGE_SIGNAL)

    async def _skip_after(self, stage: str) -> None:
        """Skip every pending stage after `stage`: the task will never get there."""
        after = STAGE_ORDER[STAGE_ORDER.index(stage) + 1:]
        for child in list(self.state.children):
            if child.stage in after:
                await self._skip(child)

    async def _ensure_stages(self, kind: str | None) -> None:
        """Once the requirements are final, the stages their kind needs: pending ones a
        kind changed in the interview no longer needs are skipped, and missing ones are
        created."""
        needed = stages_for(kind)
        for child in list(self.state.children):
            if child.stage != "requirements" and child.stage not in needed:
                await self._skip(child)
        for stage in ("architecture", "api_design"):
            if stage in needed and self._open_child(stage) is None:
                [workflow_id] = self._ids((stage,)).values()
                await self._create_pending(stage, workflow_id)
        expected = delivery_id(self._delivery_session() or "")
        current = self._open_child("delivery")
        if current is not None and current.workflow_id != expected:
            await self._skip(current)
            current = None
        if current is None:
            await self._create_pending("delivery", expected)

    def _open_child(self, stage: str) -> TaskChild | None:
        return next((child for child in self.state.children
                     if child.stage == stage and child.status in OPEN_CHILD), None)

    async def _start_pending(self, stage: str, state: Any) -> TaskChild | None:
        child = self._open_child(stage)
        if child is None or child.status != "pending":
            workflow.logger.warning("No pending %s to start", stage)
            return None
        child.status = "running"
        await self._signal(child.workflow_id, START_STAGE_SIGNAL, state)
        workflow.logger.info("Started pending %s %s", stage, child.workflow_id)
        return child

    async def _handle(self, handoff: StageHandOff) -> None:
        child = self._child(handoff.workflow_id)
        if child is not None and handoff.handed:
            child.handed = True
        task_flow_id = workflow.info().workflow_id
        if not handoff.handed:
            workflow.logger.info("Stage %s ended without handing the task on",
                                 handoff.workflow_id)
            await self._skip_after(handoff.stage)
            return
        if handoff.stage == "requirements":
            self.state.kind = handoff.kind or self.state.kind
            self._naming = asyncio.create_task(
                self._rename(self.state.task, list(handoff.requirements)))
            await self._ensure_stages(self.state.kind)
            if self.state.kind in DIRECT_KINDS and handoff.delivery is not None:
                await self._start_delivery(handoff.delivery)
            elif handoff.architecture is not None:
                architecture = handoff.architecture
                architecture.task_flow_id = task_flow_id
                architecture.pending = False
                design = self._open_child("api_design")
                architecture.next_session_id = design.workflow_id if design else None
                started = await self._start_pending("architecture", architecture)
                if started is not None:
                    await self._signal(handoff.workflow_id, NEXT_STAGE_SIGNAL,
                                       started.workflow_id)
            else:
                await self._skip_after("requirements")
        elif handoff.stage == "architecture":
            if child is not None and handoff.skipped:
                child.status = "skipped"
            if handoff.design is None:
                await self._skip_after("architecture")
                return
            design = handoff.design
            design.task_flow_id = task_flow_id
            design.pending = False
            await self._start_pending("api_design", design)
        elif handoff.stage == "api_design":
            if handoff.delivery is None:
                await self._skip_after("api_design")
                return
            await self._start_delivery(handoff.delivery)

    async def _start_delivery(self, state: DeliveryState) -> None:
        state.spec.task_flow_id = workflow.info().workflow_id
        state.spec.flow_markers = True
        state.pending = False
        current = self._open_child("delivery")
        expected = delivery_id(state.spec.session_id)
        if current is not None and current.workflow_id != expected:
            await self._skip(current)
            await self._create_pending("delivery", expected)
        await self._start_pending("delivery", state)

    async def _link_earlier_stages(self) -> None:
        """A fork links to the original task's earlier stages that are still open, which
        tell it when they close."""
        self.state.linked_open = []
        if self.state.origin != "fork":
            return
        for workflow_id in self.state.linked:
            if await self._signal(workflow_id, LINK_TASK_FLOW_SIGNAL,
                                  workflow.info().workflow_id):
                self.state.linked_open.append(workflow_id)

    async def _close_stages(self, outcome: str) -> None:
        """Close every stage that stays open after its work, with the task's outcome."""
        for child in self.state.children:
            if child.stage != "delivery" and child.status == "running":
                await self._signal(child.workflow_id, CLOSE_FLOW_SIGNAL, outcome)
            elif child.status == "pending":
                await self._skip(child)

    async def _rename(self, task: str, requirements: list[dict[str, Any]]) -> None:
        """Have the LLM name the task; its current name stays when it cannot."""
        try:
            name = await workflow.execute_activity(
                name_task_flow, args=[self.state.kind, task, requirements],
                start_to_close_timeout=NAME_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("Task not named: %s", exc.cause or exc)
            return
        if self.state.origin == "fork":
            name = task_name(name) + FORK_SUFFIX
        self.state.name = task_name(name)
        workflow.upsert_search_attributes([TASK_NAME_ATTRIBUTE.value_set(self.state.name)])
