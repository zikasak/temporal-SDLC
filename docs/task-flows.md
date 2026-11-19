# Task flows

Every new requirements interview starts a top-level **task flow** (`TaskWorkflow`,
`app/tasks/workflows.py`). The task's stages are its direct children; none of them is a
child of another stage.

| Kind of task | Stages created when the task flow starts |
| --- | --- |
| New service, API change | requirements, architecture, API design, delivery |
| Bugfix, documentation | requirements, delivery |

## Pending, started and skipped stages

A stage the task flow creates before the stages it depends on have finished is started with
`pending` set in its state and the `FlowState=pending` search attribute. It waits for the
task flow's `start_stage` signal, which carries the state it continues as a new run with,
or for `skip_stage`, which closes it with `FlowState=skipped` (`app/temporal/flags.py`).

Stages hand the task on to the task flow by their result instead of starting the next stage
themselves:

- the requirements stage returns `RequirementsResult`: the architecture to start for a new
  service or an API change, or the delivery of a bugfix or documentation task. A document
  written because the session was idle for a day starts nothing, and the later stages are
  skipped;
- the architecture stage returns `ArchitectureResult` once finalized or its skip was
  confirmed (the stage is then listed as skipped);
- the API design stage returns `DesignResult` with the delivery to start.

The task flow reserves the IDs of all its stages up front, so the requirements session
reports its `architecture_session_id` and the architecture session its
`api_design_session_id` as before. The delivery is named after the API design
(`delivery-<api design session>`), or after the requirements session for a bugfix or
documentation task, so the session endpoints keep reaching it.

The task flow closes once every child has closed, with its delivery's status: completed
when the pull request was merged (or the delivery pushed without one), cancelled otherwise,
including when the delivery was skipped.

## Names, flags and links

- `TaskName`: written by the LLM (`name_task_flow`) from the intake's "what needs to be
  done" at start and rewritten from the finalized requirements. Forks and fix rounds copy
  the original's name with a ` (fork)` or ` (fix)` suffix.
- `WaitingForUser` and `UnresolvedFindings`: each stage, delivery and onboarding keeps its
  own and reports changes to the task flow or onboarding it is a child of, which aggregates
  them. A closed flow never waits; for a closed flow GET /workflows clears the unresolved
  findings marker once an acceptance of the delivery's findings was recorded.
- A fork of a requirements, architecture or API design session starts a new task flow whose
  first stage is the fork; the earlier stages of the original task it links to are kept in
  the task flow's `linked_stages` memo and listed under it with `linked: true`.
- A fix round on a closed task's delivery (instructions about open findings, or a fix
  requested on the merged or closed pull request) runs as the delivery of a new task flow
  linked to the original task's stages; the original delivery reports it in `fix_flow_ids`.
- A task's delivery started again on its own after its task flow closed carries
  `LinkedTaskFlows=[task flow]` and stays listed under that task flow.

Onboardings keep their own top-level `OnboardingWorkflow`. Executions started before task
flows existed keep their old shape (stages under a `RequirementsWorkflow`), are listed
without a waiting highlight or marker, and are opened as before.
