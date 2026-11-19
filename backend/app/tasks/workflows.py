import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError

from app.temporal.flags import FlowFlags

with workflow.unsafe.imports_passed_through():
    from app.api_design.models import ApiDesignState, empty_api
    from app.architecture.models import ArchitectureResult, ArchitectureState
    from app.delivery.models import TASK_IDLE_TIMEOUT, DeliveryState, TaskSpec, delivery_id
    from app.requirements.models import DIRECT_KINDS
    from app.tasks.activities import name_task_flow
    from app.tasks.models import (
        DesignResult, RequirementsResult, TaskFlowState, bounded_name,
        fallback_name, stages_for,
    )
    from app.temporal.activities import register_session
    from app.temporal.closing import end_cancelled
    from app.temporal.execution_fields import (
        DELIVERY_SESSION_MEMO, LINKED_STAGES_MEMO, TASK_NAME_ATTRIBUTE, execution_fields,
        task_fields,
    )

STAGE_TYPES = {
    "requirements": "RequirementsWorkflow",
    "architecture": "ArchitectureWorkflow",
    "api_design": "ApiDesignWorkflow",
    "delivery": "DeliveryWorkflow",
}
TASK_CANCELLED = "The task ended without a delivery, or its pull request was closed."


@workflow.defn
class TaskWorkflow(FlowFlags):
    """The top-level flow of one task. It creates its stages as its children up front for
    the kind of task (requirements, architecture, API design and delivery for a new
    service or an API change; requirements and delivery for a bugfix or documentation),
    each one pending until the stages it depends on have finished, then starts it with
    what the stage before handed on, and closes the stages that will never run as skipped.
    It keeps the task's short name, aggregates its children's waiting-for-user and
    unresolved-findings flags, and closes once every child has, with its delivery's
    status: completed when the pull request was merged (or the delivery pushed without
    one), cancelled otherwise, as when the delivery was skipped.

    A fork starts a task flow whose first stage is the forked session, linked to the
    original task's earlier stages; a fix round on a closed task's delivery starts one
    whose only child is that delivery."""

    @workflow.init
    def __init__(self, state: TaskFlowState) -> None:
        self.state = state
        self._init_flags()
        self._handles: dict[str, workflow.ChildWorkflowHandle] = {}
        self._naming: asyncio.Task[None] | None = None
        # Registrations of the stages' session IDs, awaited before the task flow closes.
        self._registrations: list[asyncio.Task[None]] = []
        # A delivery stopped from outside waits here for the user to start it again.
        self._delivery_stopped = False
        self._redelivery: DeliveryState | None = None

    # The stages' workflow IDs.

    def _id(self, stage: str) -> str | None:
        if stage == "requirements":
            return self.state.requirements_session_id
        if stage == "architecture":
            return self.state.architecture_session_id
        if stage == "api_design":
            return self.state.api_design_session_id
        session = self.state.delivery_session_id
        return delivery_id(session) if session else None

    def _flags(self) -> tuple[bool | None, bool | None]:
        delivery = self._id("delivery")
        unresolved = bool(delivery and self._children_flags.get(delivery, (False, False))[1])
        return self._children_waiting(), unresolved

    @workflow.query
    def get_state(self) -> TaskFlowState:
        return self.state

    @workflow.run
    async def run(self, state: TaskFlowState) -> TaskFlowState:
        self._keep_flags()
        self._remember()
        if not self.state.name:
            self.state.name = fallback_name(self.state.task)
            self._naming = asyncio.create_task(self._name(self.state.task, []))
        self._show_name()
        first = self._first_stage()
        await self._create_stages(first)
        outcome = await self._follow(first)
        self.state.delivery_outcome = outcome
        for registration in self._registrations:
            await registration
        if self._naming is not None:
            await self._naming
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._close_flags()
        if outcome != "completed":
            workflow.logger.info("Task flow closes as cancelled (delivery %s)", outcome)
            await end_cancelled(TASK_CANCELLED)
        workflow.logger.info("Task flow completed with its delivery")
        return self.state

    # Name.

    async def _name(self, task: str, requirements: list[Any]) -> None:
        try:
            name = await workflow.execute_activity(
                name_task_flow, args=[task, requirements],
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning("Task flow not named by the LLM: %s", exc.cause or exc)
            return
        if name:
            self.state.name = bounded_name(name)
            self._show_name()

    def _show_name(self) -> None:
        workflow.upsert_search_attributes(
            [TASK_NAME_ATTRIBUTE.value_set(bounded_name(self.state.name))])

    def _rename_later(self, requirements: list[Any]) -> None:
        """Rename in the background, so naming never holds up the next stage; run()
        awaits it before the task flow closes."""
        self._naming = asyncio.create_task(self._rename(self._naming, requirements))

    async def _rename(self, previous: asyncio.Task[None] | None,
                      requirements: list[Any]) -> None:
        """Rewrite the name from the finalized requirements; a fork keeps its suffix."""
        if previous is not None:
            await previous
        if self.state.origin == "fix":
            return
        await self._name(self.state.task, requirements)
        if self.state.origin == "fork" and not self.state.name.endswith(" (fork)"):
            self.state.name = bounded_name(self.state.name, " (fork)")
            self._show_name()

    def _remember(self) -> None:
        """Keep what listings read of the task flow in its memo."""
        workflow.upsert_memo({LINKED_STAGES_MEMO: list(self.state.linked),
                              DELIVERY_SESSION_MEMO: self.state.delivery_session_id})

    # Stages.

    def _first_stage(self) -> str:
        if self.state.requirements is not None:
            return "requirements"
        if self.state.architecture is not None:
            return "architecture"
        if self.state.api_design is not None:
            return "api_design"
        return "delivery"

    def _following(self, first: str) -> list[str]:
        """The stages this task flow creates: its first and every one after it."""
        if self.state.origin == "fix" or first == "delivery":
            return ["delivery"]
        stages = (list(STAGE_TYPES) if first in ("architecture", "api_design")
                  else list(stages_for(self.state.kind)))
        return stages[stages.index(first):]

    def _placeholder(self, stage: str) -> Any:
        """The state a pending stage is created with."""
        workflow_id = workflow.info().workflow_id
        if stage == "architecture":
            return ArchitectureState(
                source_session_id=self.state.requirements_session_id or "",
                repository=self.state.repository, task_flow_id=workflow_id,
                next_session_id=self.state.api_design_session_id, pending=True)
        if stage == "api_design":
            return ApiDesignState(
                source_session_id=self.state.requirements_session_id or "",
                kind=self.state.kind, repository=self.state.repository,
                task_flow_id=workflow_id,
                architecture_session_id=self.state.architecture_session_id, pending=True)
        return DeliveryState(spec=TaskSpec(
            session_id=self.state.delivery_session_id or "", requirements=[],
            requirements_document="", api=empty_api(), kind=self.state.kind,
            repository=self.state.repository), pending=True)

    def _first_state(self, stage: str) -> Any:
        return {"requirements": self.state.requirements,
                "architecture": self.state.architecture,
                "api_design": self.state.api_design,
                "delivery": self.state.delivery}[stage]

    async def _create_stages(self, first: str) -> None:
        stages = self._following(first)
        for stage in stages:
            if stage == "delivery" and not self.state.delivery_session_id:
                # Named after the API design when the task has one, else the requirements.
                self.state.delivery_session_id = (
                    self.state.api_design_session_id if "api_design" in stages
                    else self.state.requirements_session_id)
                self._remember()
            if stage == first:
                await self._create(stage, self._first_state(stage), pending=False)
            else:
                await self._create(stage, self._placeholder(stage), pending=True)

    async def _create(self, stage: str, state: Any, pending: bool) -> None:
        workflow_id = self._id(stage)
        if not workflow_id or workflow_id in self._handles:
            return
        if stage == "delivery":
            fields = task_fields(state.spec)
            if pending:
                fields = execution_fields(state.spec.repository, pending=True)
        else:
            fields = execution_fields(self.state.repository, pending=pending)
        result_types = {"requirements": RequirementsResult,
                        "architecture": ArchitectureResult,
                        "api_design": DesignResult, "delivery": dict}
        self._handles[workflow_id] = await workflow.start_child_workflow(
            STAGE_TYPES[stage], state, id=workflow_id, result_type=result_types[stage],
            **fields)
        self.state.created[stage] = workflow_id
        if not pending:
            self.state.started.append(workflow_id)
        workflow.logger.info("Stage %s %s %s", stage, workflow_id,
                             "created, pending" if pending else "started")
        if stage != "delivery":
            self._registrations.append(asyncio.create_task(self._register(workflow_id)))

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

    async def _start(self, stage: str, state: Any) -> None:
        """Start the pending stage from `state`, creating it first if the task did not
        need it when it began, such as after the kind of task changed."""
        workflow_id = self._id(stage)
        if workflow_id not in self._handles:
            await self._create(stage, state, pending=False)
            return
        await self._handles[workflow_id].signal("start_stage", state)
        self.state.started.append(workflow_id)
        workflow.logger.info("Stage %s %s started", stage, workflow_id)

    async def _skip(self, *stages: str) -> None:
        """Close the pending stages that will never run as skipped."""
        for stage in stages:
            workflow_id = self._id(stage)
            if (not workflow_id or workflow_id not in self._handles
                    or workflow_id in self.state.started or workflow_id in self.state.skipped):
                continue
            await self._skip_id(workflow_id)

    async def _skip_id(self, workflow_id: str) -> None:
        try:
            await self._handles[workflow_id].signal("skip_stage")
        except Exception as exc:  # noqa: BLE001 - a closed stage needs no skip
            workflow.logger.warning("Stage %s not skipped: %s", workflow_id, exc)
        self.state.skipped.append(workflow_id)
        await self._closed(workflow_id)

    async def _closed(self, workflow_id: str) -> Any:
        """Wait for the stage to close; its result, or None when it did not complete."""
        try:
            return await self._handles[workflow_id]
        except ChildWorkflowError as exc:
            workflow.logger.warning("Stage %s did not complete: %s", workflow_id,
                                    exc.cause or exc)
            return None
        finally:
            if workflow_id not in self.state.closed:
                self.state.closed.append(workflow_id)

    async def _follow(self, first: str) -> str:
        """Follow the stages from `first` on; how the task's delivery ended."""
        stage = first
        if stage == "requirements":
            stage = await self._after_requirements()
        if stage == "architecture":
            stage = await self._after_architecture()
        if stage == "api_design":
            stage = await self._after_design()
        if stage == "delivery":
            return await self._follow_delivery()
        await self._skip("architecture", "api_design", "delivery")
        return "skipped"

    async def _after_requirements(self) -> str | None:
        result: RequirementsResult | None = await self._closed(self._id("requirements"))
        if not result or not result.finalized:
            # Expired, failed or stopped: no later stage will run.
            return None
        self.state.kind = result.kind
        requirements = list(result.requirements)
        if result.delivery is not None and result.kind in DIRECT_KINDS:
            await self._skip("architecture", "api_design")
            session_id = self.state.requirements_session_id
            if self.state.delivery_session_id != session_id:
                # The kind changed from one with an API design: its delivery is another.
                await self._skip("delivery")
                self.state.delivery_session_id = session_id
                self._remember()
            await self._start("delivery", result.delivery)
            self._rename_later(requirements)
            return "delivery"
        if result.architecture is not None:
            architecture = result.architecture
            architecture.task_flow_id = workflow.info().workflow_id
            architecture.next_session_id = self.state.api_design_session_id
            architecture.pending = False
            if self.state.delivery_session_id != self.state.api_design_session_id:
                await self._skip("delivery")
                self.state.delivery_session_id = self.state.api_design_session_id
                self._remember()
                await self._create("api_design", self._placeholder("api_design"), True)
                await self._create("delivery", self._placeholder("delivery"), True)
            await self._start("architecture", architecture)
            self._rename_later(requirements)
            return "architecture"
        self._rename_later(requirements)
        return None

    async def _after_architecture(self) -> str | None:
        result: ArchitectureResult | None = await self._closed(self._id("architecture"))
        if not result:
            return None
        design = result.design
        design.task_flow_id = workflow.info().workflow_id
        design.pending = False
        await self._start("api_design", design)
        return "api_design"

    async def _after_design(self) -> str | None:
        result: DesignResult | None = await self._closed(self._id("api_design"))
        if not result or result.delivery is None:
            return None
        await self._start("delivery", result.delivery)
        return "delivery"

    async def _follow_delivery(self) -> str:
        """Wait for the delivery to close: 'completed' when it did (its pull request was
        merged, or it pushed without one), 'cancelled' when its pull request was closed
        without merging, 'skipped' when it never ran. A delivery stopped from outside
        waits for the user to start it again (`restart_delivery`)."""
        workflow_id = self._id("delivery")
        while True:
            try:
                await self._handles[workflow_id]
            except ChildWorkflowError as exc:
                if isinstance(exc.cause, CancelledError):
                    self._closed_delivery(workflow_id)
                    return "cancelled"
                workflow.logger.warning("Delivery did not finish: %s", exc.cause or exc)
            else:
                self._closed_delivery(workflow_id)
                return "skipped" if workflow_id in self.state.skipped else "completed"
            self._delivery_stopped = True
            try:
                await workflow.wait_condition(lambda: self._redelivery is not None,
                                              timeout=TASK_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                self._delivery_stopped = False
                self._closed_delivery(workflow_id)
                return "stopped"
            state, self._redelivery = self._redelivery, None
            self._delivery_stopped = False
            workflow.logger.info("Starting the stopped delivery again")
            self._handles[workflow_id] = await workflow.start_child_workflow(
                "DeliveryWorkflow", state, id=workflow_id, result_type=dict,
                **task_fields(state.spec))

    def _closed_delivery(self, workflow_id: str) -> None:
        if workflow_id not in self.state.closed:
            self.state.closed.append(workflow_id)

    @workflow.update
    def restart_delivery(self, state: DeliveryState) -> None:
        """Start this task's stopped delivery again from `state`."""
        self._redelivery = state

    @restart_delivery.validator
    def validate_restart_delivery(self, state: DeliveryState) -> None:
        if not self._delivery_stopped or self._redelivery is not None:
            raise ValueError("This task's delivery cannot be started again.")
