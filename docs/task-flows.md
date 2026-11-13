# Task flows

Every task has a top-level flow of its own, a `TaskWorkflow` (`backend/app/task_flow/`),
whose ID (`task-flow-` followed by 32 hex digits) the session endpoints return as
`task_flow_id`. The stages of the task are its direct children; none is a child of another
stage.

## Starting a task flow

| Started by | First stage (running) | Pending stages | Links to |
| --- | --- | --- | --- |
| `POST /requirements/sessions` | requirements | architecture, API design, delivery (new service, API change); delivery (bugfix, docs) | — |
| Fork of a requirements session | the forked requirements session | as for a new task of its kind | — |
| Fork of an architecture session | the forked architecture session | API design, delivery | the requirements session |
| Fork of an API design | the forked API design | delivery | the requirements and architecture sessions |
| Fix round on a closed task's delivery (instructions about open findings, or a fix request mentioned on its merged or closed pull request) | the fix round's delivery (`delivery-fix-session-…`) | — | every stage of the original task |

A stage created pending is a Temporal child that waits for a `begin` signal carrying the
state it starts with, and then continues as new with it; a stage that will never run gets
`skip_stage` and closes. Pending and skipped stages carry the `FlowState` search attribute,
which `GET /workflows` reports instead of the Temporal status.

## Hand-offs

Stages no longer start each other. Each closes and hands its result to the task flow:

- requirements → the finalized requirements; the task flow renames itself from them and
  starts the architecture (new service, API change) or the delivery (bugfix, docs). A
  document written because the session was idle for a day starts nothing: the pending
  stages are skipped. Stages the task turned out to need, such as after the interview
  changed the kind of task, are created then.
- architecture → the API design to start (finalized, or with a confirmed skip, in which
  case the architecture session itself is listed as skipped);
- API design → the delivery to start;
- delivery → its outcome. A delivery stopped from outside waits up to
  `TASK_IDLE_TIMEOUT` to be started again by a retry.

The task flow closes once every stage, and every linked earlier stage of another task, has
closed: completed when its delivery completed, cancelled otherwise (including when its
delivery was skipped).

## Name

The flow starts named after the intake's task, an LLM then names it (`name_task_flow`) and
renames it from the finalized requirements (`rename_task_flow`). Forks and fix rounds copy
the original's name with a " (fork)" or " (fix)" suffix. The name is the `TaskName` search
attribute and `name` in `GET /workflows`.

## Waiting and findings

Stages, onboardings and their deliveries keep `WaitingForUser` and `UnresolvedFindings`
search attributes and report every change to the flow above them (`stage_flags` signal),
which aggregates them. A closed flow is never listed as waiting; its unresolved findings are
cleared in listings once an acceptance record exists for its delivery's last run.
Executions started before these attributes existed are listed with `null`.

## Listing

`GET /workflows?parent=<task flow>` lists its children, the linked earlier stages of another
task (`linked: true`, `parent_workflow_id` still their own task flow) and a delivery started
again on its own after the flow closed. Requirements sessions started before task flows
existed keep their old shape, with their stages as their children.
