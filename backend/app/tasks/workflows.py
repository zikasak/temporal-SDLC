import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError, ApplicationError, CancelledError, ChildWorkflowError,
)

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryState, delivery_id
    from app.tasks.activities import name_task_flow, open_workflows
    from app.tasks.models import (
        STAGE_ORDER, STAGE_TYPES, StageKind, TaskFlowState, TaskHandoff, TaskStage,
        fallback_name, placeholder, with_suffix,
    )
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        DELIVERY_MEMO, PENDING, STAGES_MEMO, TASK_NAME_ATTRIBUTE, execution_fields,
    )

# How often a flow whose own stages have all closed checks whether the earlier stages of
# another task that it links to have closed too.
LINKED_CHECK_INTERVAL = timedelta(minutes=10)
DELIVERY_CANCELLED = "The task's pull request was closed without being merged."
NO_DELIVERY = "The task ended without a delivery."


def _requirements_text(requirements: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {item.get('id', '')}: {item.get('title', '')} - "
                     f"{' '.join(str(item.get('description', '')).split())}"
                     for item in requirements)


@workflow.defn
class TaskWorkflow:
    """The top-level flow of one task. It creates every stage of the task as its own
    child at once: the first works at once, each later one waits pending until the stage
    before it hands the task on (the requirements once finalized, the architecture once
    finalized or skipped, the API design once finalized), and is closed as skipped when
    that stage ended without handing it on. The flow keeps the task's short name, stays
    open while any of its stages (and any earlier stage of another task it links to) is
    open, and then closes with its delivery's status: completed when the delivery's pull
    request was merged or nothing was left to wait for, cancelled otherwise."""

    @workflow.init
    def __init__(self, state: TaskFlowState) -> None:
        self.state = state
        self._actions: list[tuple[str, Any]] = []
        # The stage kinds that handed the task on, and the stages told to skip.
        self._handed: set[str] = set()
        self._skipping: set[str] = set()
        self._handles: dict[str, workflow.ChildWorkflowHandle] = {}
        self._redelivery: DeliveryState | None = None
        self._idle = False
        self._namings: list[asyncio.Task[None]] = []

    @workflow.run
    async def run(self, state: TaskFlowState) -> str:
        for index, stage in enumerate(self.state.stages):
            await self._create(stage, pending=index > 0)
        if not self.state.name:
            # Named by the LLM from the task; until then, by its start.
            self.state.name = with_suffix(fallback_name(self.state.task), self.state.suffix)
            self._name_from(self.state.task)
        while True:
            await workflow.wait_condition(
                lambda: bool(self._actions) or self._redelivery is not None or self._settled())
            if self._actions:
                action, value = self._actions.pop(0)
                await self._act(action, value)
                continue
            if self._redelivery is not None:
                await self._start_delivery_again()
                continue
            delivery = self._stage("delivery")
            if delivery and delivery.outcome == "failed" and not self._idle:
                # Stopped from outside, such as terminated while a stage was stuck: the
                # user may start it again from where it stopped.
                try:
                    await workflow.wait_condition(lambda: self._redelivery is not None,
                                                  timeout=TASK_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    self._idle = True
                continue
            break
        self._idle = True
        await self._await_linked()
        for task in self._namings:
            await task
        await workflow.wait_condition(workflow.all_handlers_finished)
        delivery = self._stage("delivery")
        outcome = delivery.outcome if delivery else None
        if outcome == "completed":
            workflow.logger.info("Task completed with its delivery")
            self.state.status = "completed"
            return "completed"
        self.state.status = "cancelled"
        workflow.logger.info("Task closed without a completed delivery (%s)", outcome)
        await end_cancelled(DELIVERY_CANCELLED if outcome == "cancelled" else NO_DELIVERY)

    @workflow.query
    def get_state(self) -> TaskFlowState:
        return self.state

    # Hand-offs of the stages, each starting the stage after it.

    @workflow.signal
    def requirements_finalized(self, handoff: TaskHandoff) -> None:
        self._handed.add("requirements")
        self._actions.append(("requirements", handoff))

    @workflow.signal
    def architecture_finished(self, design: ApiDesignState) -> None:
        self._handed.add("architecture")
        self._actions.append(("architecture", design))

    @workflow.signal
    def design_finalized(self, delivery: DeliveryState) -> None:
        self._handed.add("api_design")
        self._actions.append(("api_design", delivery))

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start the task's stopped delivery again from `state`."""
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        delivery = self._stage("delivery")
        if (delivery is None or delivery.state != "closed" or delivery.outcome != "failed"
                or self._redelivery is not None or self._idle):
            raise ValueError("This task's delivery cannot be started again.")

    def _stage(self, kind: str) -> TaskStage | None:
        return next((item for item in reversed(self.state.stages)
                     if item.kind == kind and not item.replaced), None)

    async def _ensure_design_stages(self, architecture_session_id: str | None) -> None:
        """Create pending architecture and API design stages, and a delivery named after
        the design, for a task whose intake named a kind without them."""
        if self._stage("architecture") is not None:
            return
        self.state.architecture_session_id = (
            architecture_session_id or f"architecture-session-{workflow.uuid4().hex}")
        self.state.api_design_session_id = f"api-design-session-{workflow.uuid4().hex}"
        for kind, workflow_id in (("architecture", self.state.architecture_session_id),
                                  ("api_design", self.state.api_design_session_id)):
            stage = TaskStage(kind=kind, workflow_id=workflow_id)
            self.state.stages.append(stage)
            await self._create(stage, pending=True)
        await self._replace_delivery(self.state.api_design_session_id)

    async def _replace_delivery(self, session_id: str) -> None:
        """Have the task's delivery named after `session_id`: a pending delivery named
        otherwise is skipped, and a new pending one takes its place."""
        current = self._stage("delivery")
        if current is not None and current.workflow_id == delivery_id(session_id):
            return
        if current is not None:
            current.replaced = True
            await self._skip_stage(current)
        self.state.delivery_session_id = session_id
        stage = TaskStage(kind="delivery", workflow_id=delivery_id(session_id))
        self.state.stages.append(stage)
        await self._create(stage, pending=True)
        workflow.upsert_memo({STAGES_MEMO: [item.workflow_id for item in self.state.stages],
                              DELIVERY_MEMO: stage.workflow_id})

    async def _skip_kinds(self, kinds: tuple[str, ...]) -> None:
        for stage in self.state.stages:
            if stage.kind in kinds:
                await self._skip_stage(stage)

    async def _skip_stage(self, stage: TaskStage) -> None:
        if stage.state != "pending" or stage.workflow_id in self._skipping:
            return
        self._skipping.add(stage.workflow_id)
        if await self._signal(stage, "skip_pending"):
            workflow.logger.info("Stage %s (%s) skipped", stage.workflow_id, stage.kind)

    async def _signal(self, stage: TaskStage, signal: str, *args: Any) -> bool:
        """Signal the stage; false when it could not be, such as when it has closed."""
        try:
            await self._handles[stage.workflow_id].signal(signal, *args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a closed stage takes no signal
            workflow.logger.warning("Stage %s not signalled %s: %s", stage.workflow_id,
                                    signal, exc)
            return False
        return True

    def _settled(self) -> bool:
        return all(item.state == "closed" for item in self.state.stages)

    def _fields(self, kind: str, pending: bool) -> dict[str, Any]:
        return execution_fields(self.state.repository, flags=True,
                                findings=kind == "delivery",
                                flow_state=PENDING if pending else None)

    def _initial(self, kind: StageKind) -> Any:
        """The start of the flow's first stage, which works at once."""
        flow_id = workflow.info().workflow_id
        state: Any = {"requirements": self.state.requirements,
                      "architecture": self.state.architecture,
                      "api_design": self.state.design,
                      "delivery": self.state.delivery}[kind]
        if state is None:
            raise ApplicationError(f"The flow has no start for its {kind} stage.",
                                   non_retryable=True)
        if kind == "requirements":
            state.task_flow_id = flow_id
            state.next_session_id = self.state.architecture_session_id
        elif kind == "architecture":
            state.task_flow_id = flow_id
            state.next_session_id = self.state.api_design_session_id
            state.pending = False
        elif kind == "api_design":
            state.task_flow_id = flow_id
            state.pending = False
        else:
            state.spec.task_flow_id = flow_id
            state.pending = False
        return state

    async def _create(self, stage: TaskStage, *, pending: bool) -> None:
        argument = placeholder(stage.kind, self.state) if pending else self._initial(stage.kind)
        handle = await workflow.start_child_workflow(
            STAGE_TYPES[stage.kind], argument, id=stage.workflow_id,
            **self._fields(stage.kind, pending))
        self._handles[stage.workflow_id] = handle
        stage.state = "pending" if pending else "running"
        stage.outcome = None
        workflow.logger.info("Stage %s (%s) created%s", stage.workflow_id, stage.kind,
                             " pending" if pending else "")
        asyncio.create_task(self._follow(stage, handle))

    async def _follow(self, stage: TaskStage, handle: workflow.ChildWorkflowHandle) -> None:
        try:
            await handle
        except ChildWorkflowError as exc:
            workflow.logger.info("Stage %s did not complete: %s", stage.workflow_id,
                                 exc.cause or exc)
            outcome = "cancelled" if isinstance(exc.cause, CancelledError) else "failed"
        else:
            outcome = "skipped" if stage.workflow_id in self._skipping else "completed"
        stage.state = "closed"
        stage.outcome = outcome
        if stage.kind != "delivery":
            self._actions.append(("closed", stage.kind))

    async def _act(self, action: str, value: Any) -> None:
        if action == "requirements":
            handoff: TaskHandoff = value
            if handoff.requirements:
                self._name_from(_requirements_text(handoff.requirements))
            if handoff.architecture is not None:
                await self._ensure_design_stages(handoff.architecture_session_id)
                architecture = handoff.architecture
                architecture.task_flow_id = workflow.info().workflow_id
                architecture.next_session_id = self.state.api_design_session_id
                await self._begin("architecture", architecture)
            elif handoff.delivery is not None:
                # A task that leaves the API as it is has no design: stages created for
                # the kind the intake named are skipped, its delivery named after the
                # requirements session.
                await self._skip_kinds(("architecture", "api_design"))
                await self._replace_delivery(handoff.delivery.spec.session_id)
                await self._begin("delivery", self._delivery(handoff.delivery))
            else:
                await self._skip_after("requirements")
        elif action == "architecture":
            design: ApiDesignState = value
            design.task_flow_id = workflow.info().workflow_id
            await self._begin("api_design", design)
        elif action == "api_design":
            await self._begin("delivery", self._delivery(value))
        elif action == "closed" and value not in self._handed:
            # The stage ended without handing the task on: nothing after it will run.
            await self._skip_after(value)

    def _delivery(self, state: DeliveryState) -> DeliveryState:
        state.spec.task_flow_id = workflow.info().workflow_id
        return state

    async def _begin(self, kind: StageKind, state: Any) -> None:
        stage = self._stage(kind)
        if stage is None or stage.state != "pending":
            workflow.logger.warning("No pending %s stage to start", kind)
            return
        state.pending = False
        if not await self._signal(stage, "begin", state):
            return
        stage.state = "running"
        workflow.logger.info("Stage %s (%s) started", stage.workflow_id, kind)
        if kind in ("architecture", "api_design"):
            try:
                await workflow.execute_activity(
                    register_session, stage.workflow_id,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=3))
            except ActivityError as exc:
                # Unrecorded, the session is still served while Temporal is up.
                workflow.logger.warning("Session %s not recorded: %s", stage.workflow_id,
                                        exc.cause or exc)

    async def _skip_after(self, kind: str) -> None:
        await self._skip_kinds(STAGE_ORDER[STAGE_ORDER.index(kind) + 1:])  # type: ignore[arg-type]

    async def _start_delivery_again(self) -> None:
        state, self._redelivery = self._redelivery, None
        stage = self._stage("delivery")
        workflow.logger.info("Starting the stopped delivery again")
        state.pending = False
        state.spec.task_flow_id = workflow.info().workflow_id
        handle = await workflow.start_child_workflow(
            STAGE_TYPES["delivery"], state, id=stage.workflow_id,
            **self._fields("delivery", False))
        self._handles[stage.workflow_id] = handle
        stage.state = "running"
        stage.outcome = None
        asyncio.create_task(self._follow(stage, handle))

    def _name_from(self, text: str) -> None:
        self._namings.append(asyncio.create_task(self._rename(text)))

    async def _rename(self, text: str) -> None:
        """Have the LLM name the flow from `text`; a failure keeps the current name."""
        if not text.strip():
            return
        try:
            name = await workflow.execute_activity(
                name_task_flow, text,
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2))
        except ActivityError as exc:
            workflow.logger.warning("The task flow was not named: %s", exc.cause or exc)
            return
        self.state.name = with_suffix(name, self.state.suffix)
        workflow.upsert_search_attributes([TASK_NAME_ATTRIBUTE.value_set(self.state.name)])
        workflow.logger.info("Task flow named %s", self.state.name)

    async def _await_linked(self) -> None:
        """Wait until the earlier stages of another task that this flow links to have
        closed too; a check that fails does not keep the flow open."""
        while self.state.linked:
            try:
                still_open = await workflow.execute_activity(
                    open_workflows, self.state.linked,
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=3))
            except ActivityError as exc:
                workflow.logger.warning("Linked stages not checked: %s", exc.cause or exc)
                return
            if not still_open:
                return
            await workflow.sleep(LINKED_CHECK_INTERVAL)
