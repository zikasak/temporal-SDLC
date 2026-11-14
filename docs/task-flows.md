# Task flows

Every task has one top-level flow, a `TaskWorkflow` (`backend/app/task_flow/`), whose
direct children are its stages: the requirements, architecture, API design and delivery
flows. Onboardings keep their own top-level `OnboardingWorkflow` and get no task flow.

## Starting

- `POST /requirements/sessions` starts a task flow (`task_flow_id`) with the interview as
  its requirements child. It creates the other children up front as pending: architecture,
  API design and delivery for a new service or an API change, delivery for a bugfix or a
  documentation change. The LLM names the flow from the intake's task
  (`name_task_flow`) and renames it from the finalized requirements.
- Forking a requirements, architecture or API design session starts a new task flow
  holding the fork, named after the original with a " (fork)" suffix, linked to the
  original task's earlier stages and with pending children for the stages after it.
- A fix round on a closed task's delivery (instructions about its open review findings,
  or a fix request mentioned on its merged or closed pull request) runs as the delivery
  child of a new task flow named with a " (fix)" suffix, linked to all of the original
  task's stages. Its ID is listed in the original delivery's `fix_flow_ids` and in the
  mention's `fix_flow_id`. An onboarding's fix round still runs as a top-level delivery.

## Stages

A pending stage waits for a `start_stage` signal with its state, then continues as new
from it; a `skip_stage` signal closes it as skipped. Each stage reports its hand-off to its
task flow (`stage_done`) instead of starting the next stage itself:

- finalized requirements start the architecture, or the delivery of a bugfix or
  documentation change; requirements written because the session was idle for a day, or a
  session that failed, skip the later stages;
- a finalized architecture, or a confirmed skip (which closes the architecture session as
  skipped), starts the API design;
- a finalized API design starts the delivery.

A kind of task changed in the interview skips the pending children it no longer needs and
creates the ones it does. Stages stay open after their hand-off until the task flow closes
them with the task's outcome. The task flow closes with its delivery's status (cancelled
when the delivery was skipped) once every child and every linked earlier stage has
closed. A delivery stopped from outside can be started again (`restart_delivery`) while
the task flow waits for it.

## Listing

GET /workflows reads these search attributes, which the worker registers at start:

| Attribute | Kept by | Listed as |
| --- | --- | --- |
| `TaskFlowId` | every stage, also a delivery started again on its own | `parent_workflow_id`, `parent=` |
| `FlowState` | pending and skipped stages | `status` pending or skipped |
| `TaskName` | task flows | `name` |
| `LinkedTaskFlows` | earlier stages a fork links to (and the `linked` memo of the fork or fix flow) | `linked` under `parent=` |
| `WaitingForUser` | stages, deliveries, onboardings; task flows aggregate their children | `waiting_for_user` |
| `UnresolvedFindings` | deliveries, onboardings and task flows | `unresolved_findings` |

A closed flow never waits; its unresolved findings marker is cleared in the listing once
the findings were accepted after it closed (`app.delivery.acceptances`). Flows started
before task flows existed carry neither marker, are listed with nulls and keep running in
their old shape, with stages under their requirements session.

The Flows page lists task flows by their name, highlights a waiting flow and each waiting
child from these fields and opens the child's view from the highlight.
