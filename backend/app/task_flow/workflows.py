import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState, empty_api
    from app.architecture.models import ARCHITECTURE_KINDS, ArchitectureState
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryState, TaskSpec, delivery_id
    from app.requirements.models import DIRECT_KINDS, RequirementsState
    from app.requirements.document import render_requirements_markdown
    from app.task_flow.activities import closed_workflows, name_task_flow
    from app.task_flow.models import (
        CLOSED_CHILD_STATUSES, STAGE_PREFIXES, STAGE_WORKFLOWS, RequirementsHandoff,
        StageChild, TaskFlowState, fallback_name, stages_of,
    )
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        PENDING, TASK_FLOW_MEMO, TASK_NAME_ATTRIBUTE, execution_fields,
    )
    from app.temporal.flow_state import (
        FLOW_ABANDONED, FLOW_CANCELLED, FlowReporting, signal_workflow,
    )

# How often the flow looks whether the earlier stages it links to have closed, besides
# being told by them.
LINKED_CHECK_INTERVAL = timedelta(hours=1)
NAME_TIMEOUT = timedelta(minutes=3)
STOPPED_DELIVERY = "The delivery was stopped and not started again."


@workflow.defn
class TaskWorkflow(FlowReporting):
    """The top-level flow of one task. It creates its stages as its children as soon as it
    knows the task needs them (requirements, architecture, API design and delivery for a
    new service or an API change; requirements and delivery for a bugfix or a
    documentation change), each one pending until the stages before it have handed the
    task on, then starts it; a pending stage that will never run is skipped. Its stages
    report their hand-offs and whether they wait for the user to it. Once its delivery has
    ended it closes its stages the same way and, after every child and every earlier
    stage of another task it links to has closed, closes with its delivery's status:
    completed when the pull request was merged, cancelled when it was closed without
    merging or when the task ended without a delivery."""

    @workflow.init
    def __init__(self, state: TaskFlowState) -> None:
        self.state = state
        self._init_reporting()
        # Hand-offs of the stages, handled one at a time in the order they arrived.
        self._events: list[tuple[str, Any]] = []
        self._follows: list[asyncio.Task[None]] = []
        # completed, cancelled or skipped, once the delivery has ended.
        self._outcome: str | None = None
        self._closing_sent = False
        # A stopped delivery waits for the user to start it again.
        self._delivery_stopped = False
        self._redelivery: DeliveryState | None = None

    @workflow.run
    async def run(self, state: TaskFlowState) -> str | None:
        self._start_reporting()
        # The first stage starts before the name is written, which may take the LLM a
        # while: a fix round's delivery is handed its mention at once.
        await self._create_stages()
        await self._name_at_start()
        await self._link()
        while not self._all_closed():
            if self._outcome is not None and not self._closing_sent:
                await self._close_stages()
                continue
            if self._events:
                name, payload = self._events.pop(0)
                await self._handle(name, payload)
                continue
            waits_for_linked = any(item not in self.state.linked_closed
                                   for item in self.state.linked)
            try:
                await workflow.wait_condition(
                    lambda: bool(self._events) or self._all_closed()
                    or (self._outcome is not None and not self._closing_sent),
                    timeout=LINKED_CHECK_INTERVAL if waits_for_linked else None)
            except asyncio.TimeoutError:
                await self._check_linked()
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._stop_reporting()
        if self._outcome == "completed":
            workflow.logger.info("Task flow completed with its delivery")
            return self.state.name
        workflow.logger.info("Task flow closing as cancelled (%s)", self._outcome)
        await end_cancelled(FLOW_CANCELLED if self._outcome == "cancelled" else FLOW_ABANDONED)
        return None

    @workflow.query
    def get_state(self) -> TaskFlowState:
        return self.state

    # What the flow reports: whether any of its stages waits for the user, and whether its
    # delivery has unresolved review findings.

    def _tracked(self) -> bool:
        return True

    def _waiting(self) -> bool:
        return any(item.waiting for item in self.state.children if item.status == "running")

    def _unresolved(self) -> bool | None:
        delivery = self._child_by_id(self.state.delivery_workflow_id)
        return bool(delivery and delivery.unresolved)

    # Naming.

    async def _name(self, text: str) -> str:
        try:
            return await workflow.execute_activity(
                name_task_flow, text,
                start_to_close_timeout=NAME_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("The task flow could not be named: %s", exc.cause or exc)
            return fallback_name(text)

    def _set_name(self, name: str) -> None:
        self.state.name = name
        workflow.upsert_search_attributes([TASK_NAME_ATTRIBUTE.value_set(name[:200])])

    async def _name_at_start(self) -> None:
        if self.state.name:
            self._set_name(self.state.name)
            return
        self._set_name(await self._name(self.state.task) + self.state.name_suffix)

    async def _rename(self, handoff: RequirementsHandoff) -> None:
        """Write the name again from the finalized requirements."""
        if not handoff.requirements:
            return
        text = render_requirements_markdown(handoff.requirements)
        self._set_name(await self._name(text) + self.state.name_suffix)
        self.state.renamed = True

    # The stages.

    def _child(self, stage: str) -> StageChild | None:
        """The stage's child that is still to run or runs, else the latest one."""
        found = [item for item in self.state.children if item.stage == stage]
        open_ = [item for item in found if item.status not in CLOSED_CHILD_STATUSES]
        return (open_ or found or [None])[-1]

    def _child_by_id(self, workflow_id: str | None) -> StageChild | None:
        return next((item for item in self.state.children
                     if item.workflow_id == workflow_id), None)

    def _new_id(self, stage: str) -> str:
        return f"{STAGE_PREFIXES[stage]}{workflow.uuid4().hex}"

    def _placeholder(self, stage: str, workflow_id: str) -> Any:
        """The input of a pending stage, until the task flow begins it."""
        flow_id = workflow.info().workflow_id
        repository = self.state.repository
        if stage == "architecture":
            kind = self.state.kind if self.state.kind in ARCHITECTURE_KINDS else "new_service"
            return ArchitectureState(kind=kind, repository=repository, status="preparing",
                                     task_flow_id=flow_id, pending=True)
        if stage == "api_design":
            return ApiDesignState(kind=self.state.kind, repository=repository,
                                  status="preparing", task_flow_id=flow_id, pending=True)
        if stage == "delivery":
            session_id = workflow_id.removeprefix("delivery-")
            return DeliveryState(spec=TaskSpec(
                session_id=session_id, requirements=[], requirements_document="",
                api=empty_api(), kind=self.state.kind, repository=repository,
                task_flow_id=flow_id, tracked=True), pending=True)
        return RequirementsState(repository=repository, kind=self.state.kind,
                                 task_flow_id=flow_id, pending=True)

    def _with_flow(self, start: Any) -> Any:
        """A stage's start, as a stage of this flow."""
        flow_id = workflow.info().workflow_id
        if isinstance(start, DeliveryState):
            start.spec.task_flow_id = flow_id
            start.spec.tracked = True
        else:
            start.task_flow_id = flow_id
        start.pending = False
        return start

    async def _start_child(self, stage: str, workflow_id: str, start: Any = None) -> StageChild:
        """Create the stage as this flow's child: running with `start`, or pending."""
        child = StageChild(stage=stage, workflow_id=workflow_id,
                           status="running" if start is not None else "pending")
        self.state.children.append(child)
        if stage == "delivery":
            self.state.delivery_workflow_id = workflow_id
        repository = self.state.repository
        if start is not None:
            start = self._with_flow(start)
            if isinstance(start, DeliveryState):
                repository = start.spec.repository or repository
            else:
                repository = start.repository or repository
        handle = await workflow.start_child_workflow(
            STAGE_WORKFLOWS[stage],
            start if start is not None else self._placeholder(stage, workflow_id),
            id=workflow_id,
            **execution_fields(repository, None, None if start is not None else PENDING),
        )
        workflow.logger.info("%s stage %s created %s", stage, workflow_id,
                             "running" if start is not None else "pending")
        self._update_memo()
        if stage != "delivery":
            await self._register(workflow_id)
            self._follows.append(asyncio.create_task(self._follow(child, handle)))
        else:
            self._follows.append(asyncio.create_task(self._follow_delivery(child, handle)))
        return child

    async def _register(self, session_id: str) -> None:
        try:
            await workflow.execute_activity(
                register_session, session_id,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            # Unrecorded, the session is still served while Temporal is up.
            workflow.logger.warning("Session %s not recorded: %s", session_id,
                                    exc.cause or exc)

    def _update_memo(self) -> None:
        workflow.upsert_memo({TASK_FLOW_MEMO: {
            "stages": [item.workflow_id for item in self.state.children],
            "linked": list(self.state.linked),
            "delivery": self.state.delivery_workflow_id,
        }})

    async def _create_stages(self) -> None:
        """The first stage runs at once; the stages after it that the task's kind needs
        are created pending."""
        if self.state.children:
            return
        first = self.state.first_stage
        starts = {"requirements": self.state.requirements,
                  "architecture": self.state.architecture,
                  "api_design": self.state.design, "delivery": self.state.delivery}
        first_id = self.state.first_stage_id
        if first == "delivery":
            first_id = first_id or delivery_id(self.state.delivery.spec.session_id)
        await self._start_child(first, first_id or self._new_id(first), starts[first])
        # The starts are handed over; the state keeps only the flow's own bookkeeping.
        self.state.requirements = self.state.architecture = None
        self.state.design = self.state.delivery = None
        stages = stages_of(self.state.kind)
        if first not in stages:
            return
        ids: dict[str, str] = {first: first_id}
        for stage in stages[stages.index(first) + 1:]:
            if stage == "delivery":
                # A bugfix or documentation change is delivered for its requirements
                # session, any other task for its API design.
                owner = ids.get("api_design") or ids.get("requirements") or first_id
                workflow_id = delivery_id(owner)
            else:
                workflow_id = self._new_id(stage)
            ids[stage] = workflow_id
            await self._start_child(stage, workflow_id)

    async def _link(self) -> None:
        """Tell the earlier stages of another task this flow links to; one that cannot be
        told has closed."""
        if not self.state.linked:
            return
        flow_id = workflow.info().workflow_id
        for workflow_id in self.state.linked:
            if workflow_id in self.state.linked_closed:
                continue
            if not await signal_workflow(workflow_id, "link_task_flow", flow_id):
                self.state.linked_closed.append(workflow_id)
        await self._check_linked()

    async def _check_linked(self) -> None:
        waiting = [item for item in self.state.linked if item not in self.state.linked_closed]
        if not waiting:
            return
        try:
            closed = await workflow.execute_activity(
                closed_workflows, waiting,
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError as exc:
            workflow.logger.warning("Linked stages not checked: %s", exc.cause or exc)
            return
        for workflow_id in closed:
            if workflow_id not in self.state.linked_closed:
                self.state.linked_closed.append(workflow_id)

    def _all_closed(self) -> bool:
        return (all(item.status in CLOSED_CHILD_STATUSES for item in self.state.children)
                and all(task.done() for task in self._follows)
                and all(item in self.state.linked_closed for item in self.state.linked))

    async def _follow(self, child: StageChild, handle: workflow.ChildWorkflowHandle) -> None:
        """Wait for a requirements, architecture or API design stage to close."""
        try:
            await handle
        except ChildWorkflowError as exc:
            workflow.logger.info("Stage %s ended: %s", child.workflow_id, exc.cause or exc)
            status = "cancelled" if isinstance(exc.cause, CancelledError) else "failed"
        else:
            status = "completed"
        child.waiting = False
        if child.status != "skipped":
            child.status = status  # type: ignore[assignment]
        if not child.handed_off and child.status != "skipped":
            # A stage that ended without reporting its end, such as one stopped from
            # outside, leaves the stages after it nothing to start from.
            self._events.append(("ended", child.stage))

    async def _follow_delivery(self, child: StageChild,
                               handle: workflow.ChildWorkflowHandle) -> None:
        """Wait for a delivery to close. One stopped from outside, such as a terminated
        one, waits for the user to start it again (`restart_delivery`)."""
        while True:
            try:
                await handle
            except ChildWorkflowError as exc:
                if isinstance(exc.cause, CancelledError):
                    # Its pull request was closed without merging.
                    outcome = "cancelled"
                    break
                workflow.logger.warning("Delivery %s did not finish: %s", child.workflow_id,
                                        exc.cause or exc)
                if child.status == "pending":
                    outcome = "skipped"
                    break
                child.status = "failed"
                child.waiting = False
                self._delivery_stopped = True
                try:
                    await workflow.wait_condition(lambda: self._redelivery is not None,
                                                  timeout=TASK_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
                self._delivery_stopped = False
                await workflow.wait_condition(workflow.all_handlers_finished)
                if self._redelivery is None:
                    outcome = "failed"
                    break
                state, self._redelivery = self._redelivery, None
                child.status = "running"
                workflow.logger.info("Starting the stopped delivery again")
                handle = await workflow.start_child_workflow(
                    "DeliveryWorkflow", self._with_flow(state), id=child.workflow_id,
                    **execution_fields(state.spec.repository))
                continue
            outcome = "skipped" if child.status in ("pending", "skipped") else "completed"
            break
        child.waiting = False
        child.status = ("skipped" if outcome == "skipped"
                        else "failed" if outcome == "failed" else outcome)  # type: ignore[assignment]
        if child.workflow_id == self.state.delivery_workflow_id and self._outcome is None:
            self._outcome = "cancelled" if outcome == "failed" else outcome
            workflow.logger.info("The task's delivery ended: %s", outcome)

    async def _skip(self, child: StageChild | None) -> None:
        if child is None or child.status != "pending":
            return
        child.status = "skipped"
        child.waiting = False
        if child.workflow_id == self.state.delivery_workflow_id:
            # The task's delivery, if it is to have one, is another one now.
            self.state.delivery_workflow_id = None
            self._update_memo()
        workflow.logger.info("Skipping the pending stage %s", child.workflow_id)
        await signal_workflow(child.workflow_id, "skip_stage")

    async def _skip_pending(self, *stages: str) -> None:
        for item in list(self.state.children):
            if item.status == "pending" and (not stages or item.stage in stages):
                await self._skip(item)

    async def _ensure(self, stage: str, workflow_id: str | None = None) -> StageChild:
        """The pending stage to begin, created now when the task turned out to need it."""
        child = self._child(stage)
        if child is not None and child.status == "pending" and (
                workflow_id is None or child.workflow_id == workflow_id):
            return child
        if stage == "delivery" and child is not None and child.status == "pending":
            # A delivery of another session, such as one created before the kind changed.
            await self._skip(child)
        return await self._start_child(stage, workflow_id or self._new_id(stage))

    async def _begin(self, child: StageChild, start: Any) -> None:
        child.status = "running"
        if child.stage == "delivery":
            self.state.delivery_workflow_id = child.workflow_id
            self._update_memo()
        if not await signal_workflow(child.workflow_id, "begin", self._with_flow(start)):
            workflow.logger.warning("Stage %s could not be started", child.workflow_id)

    # Hand-offs.

    async def _handle(self, name: str, payload: Any) -> None:
        if name == "requirements":
            await self._requirements_done(payload)
        elif name == "architecture":
            await self._architecture_done(*payload)
        elif name == "design":
            await self._design_done(payload)
        elif name == "ended":
            await self._stage_ended(payload)

    async def _stage_ended(self, stage: str) -> None:
        """A stage closed without handing the task on: nothing after it will run."""
        order = ("requirements", "architecture", "api_design", "delivery")
        later = order[order.index(stage) + 1:] if stage in order else order
        await self._skip_pending(*later)
        self._skip_closed_delivery()

    def _skip_closed_delivery(self) -> None:
        """Without a delivery that is to run, the task ends with nothing delivered."""
        if self._outcome is None and all(
                item.stage != "delivery" or item.status == "skipped"
                for item in self.state.children):
            self._outcome = "skipped"

    async def _requirements_done(self, handoff: RequirementsHandoff) -> None:
        requirements = self._child("requirements")
        if not handoff.finalized:
            # Written because the session was idle, or never written: nothing follows.
            workflow.logger.info("Requirements ended unfinalized; skipping the later stages")
            await self._skip_pending()
            self._skip_closed_delivery()
            return
        self.state.kind = handoff.kind or self.state.kind
        await self._rename(handoff)
        if handoff.kind in ARCHITECTURE_KINDS and handoff.architecture is not None:
            architecture = await self._ensure("architecture")
            design = await self._ensure("api_design")
            await self._ensure("delivery", delivery_id(design.workflow_id))
            await self._begin(architecture, handoff.architecture)
            await signal_workflow(requirements.workflow_id if requirements else None,
                                  "handed_on", architecture.workflow_id)
        elif handoff.kind in DIRECT_KINDS and handoff.delivery is not None and requirements:
            await self._skip_pending("architecture", "api_design")
            delivery = await self._ensure("delivery", delivery_id(requirements.workflow_id))
            await self._begin(delivery, handoff.delivery)
        else:
            await self._skip_pending()
            self._skip_closed_delivery()

    async def _architecture_done(self, design: ApiDesignState | None, skipped: bool) -> None:
        architecture = self._child("architecture")
        if architecture is not None and skipped and design is not None:
            # A confirmed skip: the session closes as skipped once it is handed on.
            architecture.status = "skipped"
            architecture.waiting = False
        if design is None:
            await self._skip_pending("api_design", "delivery")
            self._skip_closed_delivery()
            return
        child = await self._ensure("api_design")
        await self._ensure("delivery", delivery_id(child.workflow_id))
        await self._begin(child, design)
        if architecture is not None:
            await signal_workflow(architecture.workflow_id, "handed_on", child.workflow_id)

    async def _design_done(self, delivery: DeliveryState | None) -> None:
        design = self._child("api_design")
        if delivery is None or design is None:
            await self._skip_pending("delivery")
            self._skip_closed_delivery()
            return
        child = await self._ensure("delivery", delivery_id(design.workflow_id))
        await self._begin(child, delivery)

    async def _close_stages(self) -> None:
        """The delivery has ended: every stage closes the same way, and a pending one is
        skipped."""
        self._closing_sent = True
        cancelled = self._outcome != "completed"
        await self._skip_pending()
        for item in self.state.children:
            if item.stage != "delivery" and item.status == "running":
                await signal_workflow(item.workflow_id, "close_flow", cancelled)

    def _reported_end(self, stage: str) -> None:
        """The stage reported how it ended; its closing needs no hand-off of its own."""
        child = self._child(stage)
        if child is not None:
            child.handed_off = True

    @workflow.signal
    def requirements_done(self, handoff: RequirementsHandoff) -> None:
        """The requirements session ended, handing on its task or nothing."""
        self._reported_end("requirements")
        self._events.append(("requirements", handoff))

    @workflow.signal
    def architecture_done(self, design: ApiDesignState | None, skipped: bool) -> None:
        """The architecture session was finalized or skipped, handing on the start of the
        API design, or ended without (None)."""
        self._reported_end("architecture")
        self._events.append(("architecture", (design, skipped)))

    @workflow.signal
    def design_done(self, delivery: DeliveryState | None) -> None:
        """The API design was finalized, handing on its delivery, or ended without."""
        self._reported_end("api_design")
        self._events.append(("design", delivery))

    @workflow.signal
    def child_state(self, workflow_id: str, waiting: bool, unresolved: bool | None) -> None:
        """A stage reported whether it waits for the user and has unresolved findings."""
        child = self._child_by_id(workflow_id)
        if child is None:
            return
        child.waiting = bool(waiting) and child.status not in CLOSED_CHILD_STATUSES
        if unresolved is not None:
            child.unresolved = bool(unresolved)

    @workflow.signal
    def linked_closed(self, workflow_id: str) -> None:
        """An earlier stage of another task that this flow links to has closed."""
        if workflow_id in self.state.linked and workflow_id not in self.state.linked_closed:
            self.state.linked_closed.append(workflow_id)

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this flow's stopped delivery again from `state`."""
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        # The caller found the delivery closed; this flow may not have seen it yet.
        if not self._delivery_stopped or self._redelivery is not None:
            raise ValueError("This flow's delivery cannot be started again.")
