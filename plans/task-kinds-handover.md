# Handover: tasks that are not "build a new service"

Written on 2026-10-10 and updated on 2026-10-12 for whoever continues this work. It
assumes you have not seen the system before. All six steps are done; what is still open
is listed under each step and in section 5.

## 1. What the system is

A web application in which a user describes software in a chat and the system writes,
tests and pushes it to a GitHub repository. The backend is Python (FastAPI) and every
long-running operation is a [Temporal](https://temporal.io) workflow. The frontend is
React.

A user goes through these stages today:

1. **Requirements interview.** A chat with an LLM "business analyst" that records a list
   of requirements. The user picks a GitHub repository at the start. Pressing "Generate
   document" ends it.
2. **API design.** A second chat, started from a finished requirements session. An LLM
   "API designer" builds an HTTP API model with the user and chooses the implementation
   language. Pressing "Generate OpenAPI" ends it and writes an OpenAPI document.
3. **Delivery.** Fully automatic, no user input:
   1. *Code generation*: `openapi-generator` turns the OpenAPI document into interfaces
      and models.
   2. *Implementation* and *test generation* run in parallel, each an LLM coding agent in
      its own Docker container. The implementation agent writes the service. The test
      agent writes an HTTP test suite **from the contract only**: it never sees the
      implementation, and the implementation agent never sees the tests.
   3. *Verification*: the service image and the test-runner image are started together
      and the tests run against the live service.
   4. *Repair loop*: if tests fail, a diagnosis agent decides whether the service or the
      tests are wrong, that side is sent back for repair, and verification runs again
      (up to 5 rounds).
   5. *Publish*: if verification passed, the sources are pushed to a new `generated/…`
      branch of the repository.

Vocabulary used below:

| Term | Meaning |
|---|---|
| Session | One chat (requirements or API design). Its ID is the Temporal workflow ID. |
| Contract | The requirements plus the OpenAPI document. The only authority for both coding agents. |
| Contract tests | The HTTP test suite the test agent writes. Lives in `contract-tests/` in the repository. |
| Checkout | The clone of the user's repository on the worker's disk, one per session. |
| Delivery | Stage 3 above as a whole. |
| Language | A registered stack (today only Java/Spring Boot) with its templates and build commands. |

### Where things are

| Path | What it holds |
|---|---|
| `backend/app/temporal/workflows.py` | `RequirementsWorkflow` and `ApiDesignWorkflow` (the two chats) |
| `backend/app/requirements/`, `backend/app/api_design/` | Prompts, state models and HTTP-facing services of the two chats |
| `backend/app/tools/` | The tools the chat LLMs can call (`record_requirement`, `set_language`, …) |
| `backend/app/delivery/` | `DeliveryWorkflow`, its `TaskSpec` / `DeliveryProgress` models, and the manual retry |
| `backend/app/codegen/`, `implementation/`, `test_generation/`, `verification/`, `diagnosis/`, `documentation/` | One delivery stage each: a child workflow, its activity, and (for the agents) the in-container `agent.py` with its prompt |
| `backend/app/agent_workflows/` | Machinery shared by the coding agents: container lifecycle, tool loop, contract files |
| `backend/app/languages/` | The language registry; `java/` is the only entry |
| `backend/app/github/checkout.py`, `backend/app/temporal/activities.py` | Clone and push (`checkout_repository`, `publish_repository`) |
| `backend/app/api/routes/` | HTTP routes |
| `frontend/src/Requirements.tsx`, `ApiDesign.tsx` | The two chat pages |
| `frontend/src/Delivery.tsx` | Delivery progress, rendered by both pages |
| `docs/service-implementation.md` | The detailed reference for delivery. Read it before changing a stage. |

### Running the tests

The worktree has no Python environment of its own. Use the one in the main checkout:

```sh
cd backend
/Users/zikasak/programming/temporal-project/backend/env/bin/python -m pytest tests -q
```

The suite takes about 40 seconds: 333 passed, 11 skipped (the skipped ones need Docker).
If a run produces no output for more than a minute it is hanging, not slow; see
"Traps" at the end. The frontend type-check is `npx tsc --noEmit -p .` in `frontend/`
(it needs `node_modules`, which also exists only in the main checkout).

## 2. The problem

Before this work every stage assumed the task is "build a new service from nothing"
(a bugfix no longer goes this way, see step 4):

- the requirements interview asks what the user wants to build and covers a whole system;
- API design always runs, starts from an empty API, and is the only door into delivery;
- code generation regenerates the whole server and overwrites the repository's contract
  files;
- the implementation agent is told to "implement the service" and must produce a
  Dockerfile;
- the test agent writes a complete suite and cannot target one behaviour;
- "verified" means only "the full suite passes".

A bugfix, a small change or a documentation edit fits none of this.

## 3. Decisions already made

These were agreed with the project owner. Do not reopen them without asking.

1. **Only repositories this pipeline created are supported.** The layout (service
   sources, `contract-tests/`, a Dockerfile serving on port 8080) can be assumed.
2. **No fallbacks or backward compatibility.** There are no users. Do not add code paths
   for repositories, sessions or workflow histories from before a change. Old sessions
   are allowed to stop working.
3. **The agent must ask the human what kind of task this is and never assume it.** It may
   not infer the kind from the conversation or the repository.
4. **The implementation agent must never see tests**, and the test agent must never see
   the implementation. This independence is the core of the design and holds for every
   task kind.
5. **A bugfix is verified red → green**, and the whole existing suite runs too, not only
   the new tests.
6. **Documentation-only tasks get a placeholder verification** for now. Their changes are
   still pushed, marked as unverified. Real verification comes later.

### Task kinds

| Kind | Interview | API design + code generation | Authoring | Verification |
|---|---|---|---|---|
| New service | As today | As today | As today | Full suite |
| API change (new endpoint, changed field) | Only what changes | Starts from the repository's stored contract | As today, inside the repository | Full suite |
| Bugfix / other non-API change | Short brief: problem, expected behaviour, acceptance criteria | Skipped | Test agent adds regression tests; implementation agent fixes | Red → green, below |
| Docs-only | Short brief | Skipped | An agent limited to documentation files | Placeholder: reports "not implemented" |

Without a selected repository only "new service" is valid, but the agent still asks.

### Red → green for a bugfix

1. The test agent adds regression tests to the existing `contract-tests/` suite, working
   from the brief and the stored contract, without seeing the implementation.
2. **Baseline run** against the service as it is in the repository: the new tests must
   fail and every existing test must pass.
3. If a new test passes at baseline, the bug was not reproduced. Stop and report this to
   the user; do not let the implementation agent "fix" anything.
4. The implementation agent fixes the service from the brief and the contract.
5. **Final run**: the whole suite, old and new, must pass. Failures go through the
   existing diagnosis and repair loop.

## 4. What is done

### Step 1: delivery as its own workflow

Delivery used to be a set of methods inside `ApiDesignWorkflow`, so nothing could reach
it without an API design. It is now its own workflow:

- `backend/app/delivery/models.py`: `TaskSpec` (what to build: session ID, requirements,
  API model, OpenAPI document ID, language, repository, checkout), `DeliveryProgress`
  (each stage's result or error) and `DeliveryState` (both, plus the stage being retried).
- `backend/app/delivery/workflows.py`: `DeliveryWorkflow`. Its workflow ID is
  `delivery-<session id>`.
- `backend/app/delivery/service.py`: reading progress, and the manual retry of a failed
  stage, which starts a new run of the same delivery.
- `ApiDesignWorkflow` only designs. After writing the OpenAPI document it starts the
  delivery as a child workflow and **waits for it** (`_deliver`).
- `GET /api/api-design/sessions/{id}` returns the same JSON as before: the service merges
  the delivery's progress into the session, so the frontend was not touched apart from
  the Workflows list.

The Temporal replay patches for the moved stages were deleted along with two tests of
them, per decision 2.

### Step 2: the task kind

- `TaskKind` and `TASK_KINDS` in `backend/app/requirements/models.py`: `new_service`,
  `api_change`, `bugfix` (also any other non-API change) and `docs`.
- `RequirementsState.kind` starts unset. The interviewer records it with the
  `set_task_kind` tool (`backend/app/tools/requirements.py`); `requirements_turn` takes the
  kind and returns it with the requirements.
- The opening message asks for the kind. `kind_guidance` in
  `backend/app/requirements/prompt.py` tells the interviewer never to assume it while it
  is unset, and afterwards selects the interview: the full one for a new service, a short
  brief for the others.
- `validate_finalize` refuses while the kind is unset, and an idle session without a kind
  fails instead of writing a document.
- The kind is carried into `ApiDesignState.kind` and `TaskSpec.kind`. `bugfix` (step 4)
  and `docs` (step 6) have their own paths through the delivery; an API change is built
  as a new service inside the repository (step 5).
- The requirements page shows the kind and disables "Generate document" until it is set.

Not done: "without a selected repository only new service is valid". A requirements
session cannot be started without a repository today, so there is nothing to enforce;
revisit it if that changes. A fork keeps its source's kind even when the fork point
precedes the choice, as forks do for the language.

### Step 3: the contract in the repository

Every later kind starts from the contract of the repository it changes, so a delivery
now publishes it:

- `backend/app/delivery/contract.py`: `RepositoryContract` (language, requirements,
  requirements document, API model), `write_contract` and `read_contract`.
- `publish_repository` takes the contract instead of the language and writes it to
  `contracts/` in the checkout before the push: `language.json`, `requirements.json`,
  `requirements.md`, `api.json` (the structured model, which is what gets read back) and
  `openapi.yaml` (rendered from the model, for readers). The directory is replaced whole.
- `is_service_file` excludes `contracts/`, so the implementation agent never sees or
  writes it. The test workspace only ever took `contract-tests/` from the checkout.
- The language is stored because a task that skips API design has nowhere else to get it.

`read_contract` raises `ContractError` for a repository without a contract (decision 2).
Step 4 calls it through the `read_repository_contract` activity; step 5 is its other
caller and must report that error to the user too.

### Step 4: the bugfix path

The reference is "Changing an existing service (bugfix)" in
`docs/service-implementation.md`. In short:

- **Entry.** `RequirementsWorkflow` reads the repository's contract when a session of a
  kind in `CONTRACT_KINDS` (`backend/app/requirements/models.py`: `api_change`, `bugfix`
  and, since step 6, `docs`) is finalized and keeps it in `RequirementsState.contract`.
  For a kind in `DIRECT_KINDS`
  (`bugfix` and `docs`) it then starts the `DeliveryWorkflow` once the document is written
  and waits for it (`_deliver`). A repository without a
  contract refuses the finalize and the session shows why. An API design cannot be
  started from such a session.
- **`TaskSpec`.** Its contract fields hold the repository's contract; `brief` holds the
  interview's requirements and document. `document_id` is unset.
- **`DeliveryWorkflow._change`.** Test agent, baseline (`_reproduce`), implementation
  agent, then the usual `_verify_built`. `judge_baseline` in `delivery/models.py` decides
  `baseline_status`; only `reproduced` goes on.
- **Baseline image.** `build_baseline` in `verification/activities.py`, on the
  verification queue, builds the checkout as the implementation agent sees it.
- **New tests and old.** Tests are tracked one by one, not by file: `TestSuite.test_cases`
  and `case_id` in `backend/app/languages/`. The test agent may add tests anywhere and
  change none; the stage reports `added_tests`, and verification reports each named test
  on its own (`VerificationResult.tracked`).
- **Which tests a repair may change** (every kind of task). The diagnosis verdict names
  the failing tests that are themselves wrong (`tests`); the test agent may change or
  remove those and no others, and the stage rejects the suite otherwise. A repository
  test corrected this way (`changed_tests`) may fail at the baseline; an added test must
  still fail there.
- **Prompts.** `CHANGE_PROMPT` in `implementation/agent.py`, `test_generation/agent.py`
  and `diagnosis/agent.py`, selected by the `brief` entry of the stage context.
- **Contract.** A published bugfix appends the brief's requirements to the repository's
  contract and drops the ones it retired (`with_requirements` in `delivery/contract.py`), so a later task's test agent
  keeps the regression tests. The interview numbers a bugfix's requirements past the contract's
  from the start (`RequirementsContext.published` in `tools/requirements.py`); ones
  recorded before the kind was set are renumbered then (`numbered_after`) and the analyst
  announces it. Finalizing applies the rule again as a guard.
- **Reporting.** `DeliveryProgress.baseline*`; `frontend/src/Delivery.tsx` renders delivery
  progress for both pages. The requirements session API returns it as `delivery` and has
  its own `generation/retry` route.
- **Tests.** `backend/tests/pipeline.py` holds the stage fakes; the pipeline tests are in
  `backend/tests/test_delivery_workflow.py`, entered directly or through a bugfix session.

Beyond what the plan asked for:

- A repair of the *tests* in the final loop runs the baseline again. Without this a
  repaired added test could pass with and without the fix.
- Retrying `tests` is also the retry of a failed or unreproduced baseline, and of a
  verification that ended with an error.
- The final run of a bugfix must run every test: a skipped or unreported test leaves a
  passed run unpublished (`unproven_green` in `delivery/models.py`).

Not done, and open:

- The bugfix path has only run against fake stages and the unit tests of each stage. It
  has not been run end to end with real agents and Docker.
- Only test methods, with their own annotations, are protected from change. A repair can
  still alter a shared helper or fixture, or annotate a whole class, which changes what
  untouched tests do, and the build file can leave tests out of the run. For a bugfix
  the baseline catches both, because it refuses a run with a skipped test or with a test
  of the suite's sources missing from the reports. An API change has the same
  rule for its final run; a new service's verification still passes with skipped tests.
- Test identity relies on Surefire naming a report entry after its method. A test with a
  custom display name for its invocations may not be matched, which ends a baseline as
  "did not run" rather than passing silently.

### Step 5: the API change path

The reference is "Changing an existing API" in `docs/service-implementation.md`. In short:

- **Entry.** An `api_change` requirements session reads the contract at finalize like a
  bugfix (same refusal, same numbering of its requirements past the contract's) but does
  not deliver.
- **Design.** `start_session` in `backend/app/api_design/service.py` seeds the design
  with the contract's API and language and with the contract's requirements plus the
  change's. `ApiDesignState.change_document` holds the change's own document and marks
  the session as a change.
- **Prompt.** `CHANGE_GUIDANCE`, `CHANGE_KICKOFF_MESSAGE` and `LANGUAGE_FIXED` in
  `backend/app/api_design/prompt.py`. `set_language` refuses in such a session.
- **Delivery.** Unchanged: the new-service path in the repository. The test agent's
  `REPOSITORY_PROMPT` tells it to keep the existing tests the new contract supports.

- **Retired requirements.** The interviewer of a change sees the contract's requirements
  and ends the ones the change contradicts with the `retire_requirement` tool
  (`keep_requirement` undoes it). `RequirementsState.retired` carries them, and
  `with_requirements` drops them from the contract and from the endpoints' links. This
  holds for a bugfix too, whose brief passes them to the agents as `retired`.
- **Snapshot of the tests.** Every delivery with a checkout first copies the repository's
  tests to `<session>/repository-tests/` (`snapshot_repository_tests`, once). The test
  stage and every comparison read that copy, not the checkout.
- **Kept tests.** A change's verification must run every test (`unproven_green`, now for
  both change kinds). For an API change, each repository test the author changed or
  removed is then run as it was against the changed service; one that still passes blocks
  the publication (`_prove_kept` in `DeliveryWorkflow`, `judge_kept`).
- **Moved repository.** The contract is kept with the commit it was read at. The design
  session compares its own checkout's commit and, when it differs, the contract itself:
  the same contract goes on, a different one fails the session (`_confirm_contract`).

Not done, and open:

- Like the bugfix path, it has only run against fake stages.
- A kept-tests violation ends the delivery; the user retries `tests`. Nothing restores
  the tests automatically, and the retried author is not told which ones it must keep.
- In the kept-tests run a test that was skipped counts as still passing.
- Helpers, fixtures and build files are still not protected, as for a bugfix.
- A moved repository is judged by its contract alone. Service code or tests pushed in
  between are not compared; the delivery works from the newer checkout.
- The requirements session's own interview is not rechecked: a session left open while
  the branch moves still reads the contract of its first clone.

### Step 6: the docs-only path

The reference is "Changing documentation only" in `docs/service-implementation.md`. In
short:

- **Entry.** A `docs` requirements session reads the contract at finalize (same refusal
  as a change) and delivers itself, like a bugfix. `CHANGE_KINDS` still means "changes
  the service": a docs task is not in it, so its requirements are not renumbered, it
  retires nothing, and the contract is published by nobody.
- **The author.** `backend/app/documentation/`: `DocumentationWorkflow`,
  `write_documentation` and the in-container `agent.py`, on their own queue and worker
  (`python -m app.documentation.worker`; added to the dev stack, the Zed profiles and
  `docker-compose.coolify.yml`). `is_documentation_file` decides what the agent is shown
  and what is taken back: `.md`, `.markdown`, `.rst` and `.adoc` files outside
  `contracts/`, `contract-tests/` and build output. The stage fails if nothing changed.
- **No image.** `StageSpec.image_label` may now be `None`: `run_stage` then publishes the
  output as files and its result has no `image_id`.
- **Verification.** `verify_documentation` in `verification/activities.py`, on the
  verification queue, is the strategy for this kind. It starts nothing and returns a
  `VerificationResult` with `status: not_implemented`. The real check replaces its body;
  `DeliveryWorkflow._document` and `_publish` already handle `passed`, `failed` and
  `error` from it.
- **Publish.** `_publish` lets exactly one case through without a pass: kind `docs` with
  a completed `not_implemented` result. `publish_documentation`
  (`temporal/activities.py`) replaces the documentation files of the checkout and
  nothing else. The commit subject ends in `(unverified)` and
  `RepositoryPublication.verified` is false; it is true for every other publication.
- **Retry.** The new retry stage `documentation`.
- **Page.** `DeliveryResults` takes `documentation` (the requirements page passes it for
  a docs session) and shows the changed files, "Not verified", and the note on the
  branch.

Not done, and open:

- Like the other two paths, it has only run against fake stages and the stage's own unit
  tests, never with a real agent and Docker.
- The author sees no code. It documents the service from the contract, the brief and the
  existing documentation, so a brief that needs facts found only in the code (a
  configuration key, a build command) cannot be met accurately.
- Documentation is recognised by file suffix alone. Images and other assets under
  `docs/`, and plain-text files, are neither shown to the author nor publishable.
- The author's image is the language's full toolchain image, which it does not need.
- "Without a selected repository only new service is valid" still has nothing to
  enforce (see step 2).

## 5. What remains

The six planned steps are done. What is left is the open items listed under steps 4, 5
and 6. The two that matter most:

- **Run each path end to end with real agents and Docker.** None of the bugfix, API
  change and docs paths has run outside fake stages.
- **Real verification for documentation.** Replace the body of `verify_documentation`.
  When it returns `passed` the publication is marked verified with no other change.

## 6. Traps

- **Do not let a session workflow finish while its delivery child is still running.** The
  first version started the delivery with `ParentClosePolicy.ABANDON` and returned. The
  Temporal time-skipping test server then stops advancing time, and every test that waits
  for the idle timeout hangs forever. This is why `ApiDesignWorkflow` and
  `RequirementsWorkflow` wait for their delivery.
- **Workflow code must be deterministic.** Anything that reads the clock, the disk or the
  network belongs in an activity. Changing the order of commands in a workflow breaks
  replay of its existing histories; that is acceptable here (decision 2) but it means
  sessions started before a deploy can stop loading.
- **Workflow inputs and query results are dataclasses** converted by Temporal. Give every
  new field a type hint and, unless it is required, a default.
- **Queries of a delivery that has not started return "not found".**
  `delivery.service.get_progress` turns that into `None`; keep doing so for any new entry
  point.
- **Artifacts on disk are keyed by the session ID**, not the delivery ID:
  `<GENERATED_CODE_DIR>/<session-id>/…`. Always pass `TaskSpec.session_id` to stages.
- **The published branch name is derived from the session ID** in
  `publication_branch` (`backend/app/github/checkout.py`): what follows `-session-`,
  whichever kind of session started the delivery.
