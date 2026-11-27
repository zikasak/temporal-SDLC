# Task flows

Every new requirements interview and every onboarding starts a top-level **task flow**
(`TaskWorkflow`, `app/tasks/workflows.py`). The task's stages are its direct children; none
of them is a child of another stage. The task flow carries the `TaskOrigin` search attribute
(`requirements` or `onboarding`), which GET /workflows returns as `origin` and filters on.

A requirements session starts from the repository, its branch and what needs to be done
alone; its task flow creates only the requirements stage at start. The interviewer proposes
the kind of task and records it (`set_task_kind`) once the user confirms it or names another;
it can change until the session is finalized, and finalizing without a kind answers 409.
When the session is finalized (with the first review round, or with the document of a
documentation task), the task flow creates the other stages for the kind recorded then:

| Kind of task | Stages created at finalize |
| --- | --- |
| New service, API change | requirements review, architecture, API design, delivery |
| Bugfix | requirements review, delivery |
| Documentation | delivery |
| Onboarding | onboarding interview, requirements review, delivery (all at start) |

A session written because it was idle for a day creates no further stages, and its task
flow closes as cancelled. A fork of a requirements session starts like a new session, with
the kind recorded at the fork point; a fork of an architecture or API design session gets
pending stages for the stages after it at once.

## The requirements review

The requirements review (`RequirementsReviewWorkflow`, `app/requirements_review/`) is named
after the interview it checks: `requirements-review-<id>` for `requirements-session-<id>`,
`onboarding-review-<id>` for `onboarding-session-<id>`. It runs every round in one
execution, with the `requirements_review` model settings.

- Finalizing a new service, API change or bugfix (or a full onboarding) hands the first
  round to the task flow (`review_round` signal), which starts the pending review with it.
  The interview's status is `finalizing` while a round runs.
- Each round an LLM (`review_requirements` activity) checks the requirements against each
  other and against the existing service's requirements the task does not retire (for an
  onboarding that replaces a contract, that contract's). From the second round on it is
  given the diff of the requirements since the previous round and the previous round's
  contradictions with the user's answers, or marked unanswered. At most five contradictions
  are reported per round; IDs (`C-1`, `C-2`, …) stay stable across rounds.
- The review signals the result back (`review_result`). Contradictions are asked as one
  assistant message with one question per contradiction. Answering them runs an interviewer
  turn that applies the answers, then the next round; a free message drops them, and
  finalizing again starts the next round. Rounds are unlimited.
- A round whose activity fails for good is reported `failed`; `POST …/review/retry` runs it
  again with the same input, finalizing starts a new round on the current requirements.
- When a round finds no contradictions the review closes as passed and the document is
  written. `finalize?force=true` writes it at once whatever the review's state: a running
  round is cancelled, and the review closes as continued anyway with the last finished
  round's contradictions, which the requirements document lists.
- Documentation tasks, architecture-only onboardings and sessions written because they were
  idle are not reviewed: the review is skipped (or never created for documentation).

## Pending, started and skipped stages

A stage the task flow creates before the stages it depends on have finished is started with
`pending` set in its state and the `FlowState=pending` search attribute. It waits for the
task flow's `start_stage` signal, which carries the state it continues as a new run with,
or for `skip_stage`, which closes it with `FlowState=skipped` (`app/temporal/flags.py`).

Stages hand the task on to the task flow by their result instead of starting the next stage
themselves. A stage does not close as it hands its result on: it sends it with the task
flow's `stage_result` signal, is listed with `FlowState=completed` (or stays `skipped`), and
stays open, still answering its queries, until the task flow sends `release_stage` as it
closes (`OpenStage` in `app/temporal/flags.py`). The stage then closes with the same
result. Retention only deletes closed workflows, so no stage of a running task is deleted
and every session page stays readable while the later stages run. The stages are:

- the requirements stage returns `RequirementsResult`: the architecture to start for a new
  service or an API change, or the delivery of a bugfix or documentation task, and whether
  the review decided (otherwise the task flow closes it as skipped). A document written
  because the session was idle for a day starts nothing, and the later stages are skipped;
- the onboarding interview returns `OnboardingResult` with the delivery of its contract;
- the architecture stage returns `ArchitectureResult` once finalized or its skip was
  confirmed (the stage is then listed as skipped);
- the API design stage returns `DesignResult` with the delivery to start.

The task flow reserves the IDs of all its stages up front, so the requirements session
reports its `architecture_session_id` and the architecture session its
`api_design_session_id` as before. The delivery is named after the API design
(`delivery-<api design session>`), or after the requirements session for a bugfix or
documentation task, so the session endpoints keep reaching it.

The task flow closes once every child has closed (it releases the stages that handed on
their results and waits for them first), with its delivery's status: completed
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

Executions started before task flows existed keep their old shape (stages under a
`RequirementsWorkflow`), are listed without a waiting highlight or marker, and are opened as
before. Top-level `OnboardingWorkflow`s from before onboardings ran under task flows are
cancelled once when the API starts (`cancel_old_shape_onboardings`); they stay listed and
readable.
