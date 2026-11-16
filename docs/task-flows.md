# Task flows

Every task the user starts with a requirements interview is one top-level Temporal workflow, a
`TaskWorkflow` (`backend/app/task_flow/`). Its requirements, architecture, API design and
delivery stages are all its direct children; none of them is a child of another stage.

## Stages

- When the task flow starts, it starts its first stage and creates the stages after it at once
  as **pending** children: requirements, architecture, API design and delivery for a new
  service or an API change; requirements and delivery for a bugfix or a documentation change.
  A pending stage carries the `FlowState=pending` search attribute and waits for the `begin`
  signal with its real start.
- A stage reports its hand-off to the task flow instead of starting the next stage itself:
  the requirements session when the user generated its document (`requirements_done`), the
  architecture session when it was finalized or its skip confirmed (`architecture_done`), the
  API design when it was finalized (`design_done`). The task flow then begins the pending
  next stage and tells the earlier one which stage it started (`handed_on`), which the
  session responses report as `architecture_session_id` and `api_design_session_id`.
- A pending stage that will never run, such as the stages after a requirements session whose
  document was written because it was idle for a day, or an architecture stage whose skip
  was confirmed, closes as **skipped** (`FlowState=skipped`).
- The delivery of a bugfix or documentation change is `delivery-<requirements session>`, that
  of any other task `delivery-<API design session>`, as before; it is a child of the task
  flow. A delivery stopped from outside is started again through the task flow's
  `restart_delivery` update.
- Once the delivery has ended (its pull request merged or closed, or pushed without a pull
  request), the task flow closes its stages the same way (`close_flow`) and, after every
  child and every linked earlier stage has closed, closes with the delivery's status:
  completed, or cancelled when the pull request was closed without merging or the task ended
  without a delivery.

## Name

The task flow's short name (`TaskName` search attribute, `name` in `GET /workflows`) is
written by the LLM (`name_task_flow`) from the intake's "what needs to be done" and rewritten
from the finalized requirements. When the LLM cannot answer, the start of the task is used.

## Forks and fix rounds

- Forking a requirements, architecture or API design session starts a new task flow named
  after the original with a " (fork)" suffix. It holds the forked session and pending
  children for the later stages, and links to the original task's earlier stages: these are
  listed under `GET /workflows?parent=<fork's task flow>` with `linked: true`, and the fork
  closes only once they have closed too. The original task flow is left unchanged.
- A fix round on a closed task's delivery (instructions about open review findings, or a fix
  requested by a mention on the merged or closed pull request) runs as the delivery child of
  a new task flow named with a " (fix)" suffix, linking to the original task's stages. Its ID
  is listed in the original delivery's `fix_flow_ids`, and as the mention's `fix_flow_id`.
  Onboardings keep their own top-level flows, without a task flow.

## Waiting for the user and unresolved findings

Stages, onboardings and their deliveries keep the `WaitingForUser` and `UnresolvedFindings`
search attributes as their state changes and signal them to their parent (`child_state`); the
task flow and the onboarding aggregate their children's. `GET /workflows` returns them as
`waiting_for_user` (always false for a closed flow) and `unresolved_findings` (cleared for a
closed flow once its findings were accepted); both are null for executions started before
they existed. The Flows page highlights and marks flows from these fields.

Executions started before task flows existed keep running in their old shape: a requirements
session that starts its architecture and API design as its own children.
