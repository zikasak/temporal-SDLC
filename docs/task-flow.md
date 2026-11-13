# The task flow

Every task specified in a requirements interview runs as one top-level `TaskWorkflow`
(`backend/app/tasks/`), whose ID (`task-flow-…`) the sessions return as `task_flow_id`.
Its requirements, architecture, API design and delivery are all its direct children; none
of them is a child of another.

## Children

- `POST /requirements/sessions` starts the task flow, which starts the interview as its
  requirements child and creates its later stages at once as pending children:
  architecture, API design and delivery for a new service or an API change, delivery for a
  bugfix or a documentation change. A pending child is a real execution of its workflow
  type that waits until the task flow begins it with its state (signal `begin_stage`); it
  then continues as a new run from that state (`app/temporal/flags.py`, `PendingStage`).
- When the user generates the requirements document, the requirements child returns what
  the next stage starts from, and the task flow begins it: the architecture of a new
  service or an API change, or the delivery of a bugfix or documentation task. The
  architecture hands the API design on, and the finalized design its delivery, the same
  way. A child the task turns out not to need, such as when the interview changed the
  kind of task, is skipped (signal `skip_stage`) and one it needs is created then.
- A document written because the session was idle for a day starts no stage: the pending
  children close as skipped, as do the stages after any stage that failed or was
  abandoned. An architecture session whose skip the user confirmed is listed as skipped
  too, and its API design begins with the repository's existing architecture.
- A delivery stopped from outside waits for the user to start it again
  (`restart_delivery`) as the task flow's child, as sessions did before.
- The task flow stays open until every child has closed, and closes with its delivery's
  status: completed when the pull request was merged, cancelled otherwise.

The LLM names the flow from what needs to be done when it starts and again from the
finalized requirements (`name_task_flow`); until it has, the flow is named after the task's
own words.

## Forks and fix rounds

Forking a requirements, architecture or API design session starts a new task flow named
after the original with a " (fork)" suffix. It holds the forked session, links to the
original task's earlier stages and has pending children for the stages after it; the
original task flow stays as it is. Sessions of a flow that has closed cannot be forked.

A fix round on a closed task's delivery (instructions about open review findings, or a fix
requested by a mention on its merged or closed pull request) runs as the delivery child of
a new task flow (`task-flow-<hex>` of its `fix-session-<hex>`) named with a " (fix)"
suffix, which links to the original task's stages. The closed delivery lists these flows as
`fix_flow_ids`. An onboarding's fix round stays a top-level `DeliveryWorkflow` of its own.

## Listing

`GET /workflows` lists task flows with type `TaskWorkflow`, and with `top_level=true`
instead of their stages. `parent=<task flow>` lists its children, the delivery started again
on its own and the linked earlier stages of another task (`linked: true`). Each execution
also carries:

- `status` `pending` or `skipped` for a stage that waits or will never run, from the
  `flow_status` memo;
- `name` of a task flow;
- `waiting_for_user` and `unresolved_findings`, which the workflows keep in their memo as
  they change; a stage reports them to its task flow, and a delivery to its onboarding, so
  the parent reports them for its whole tree. A closed flow waits for nothing, and its
  unresolved findings are cleared by an acceptance given after it closed.

Executions started before these fields were reported list them as null. Requirements
sessions started before task flows existed keep their own structure: they start their
architecture and API design as their children.
