# Handover: tasks that are not "build a new service"

Written on 2026-10-10 for whoever continues this work. It assumes you have not seen the
system before. Step 1 of six is done and uncommitted on branch `t3code/c29d2fba`; the rest
is described below in the order it should be built.

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
| `backend/app/codegen/`, `implementation/`, `test_generation/`, `verification/`, `diagnosis/` | One delivery stage each: a child workflow, its activity, and (for the agents) the in-container `agent.py` with its prompt |
| `backend/app/agent_workflows/` | Machinery shared by the coding agents: container lifecycle, tool loop, contract files |
| `backend/app/languages/` | The language registry; `java/` is the only entry |
| `backend/app/github/checkout.py`, `backend/app/temporal/activities.py` | Clone and push (`checkout_repository`, `publish_repository`) |
| `backend/app/api/routes/` | HTTP routes |
| `frontend/src/Requirements.tsx`, `ApiDesign.tsx` | The two chat pages; `ApiDesign.tsx` also renders delivery progress |
| `docs/service-implementation.md` | The detailed reference for delivery. Read it before changing a stage. |

### Running the tests

The worktree has no Python environment of its own. Use the one in the main checkout:

```sh
cd backend
/Users/zikasak/programming/temporal-project/backend/env/bin/python -m pytest tests -q
```

The suite takes about 35 seconds: 275 passed, 11 skipped (the skipped ones need Docker).
If a run produces no output for more than a minute it is hanging, not slow; see
"Traps" at the end. The frontend type-check is `npx tsc --noEmit -p .` in `frontend/`
(it needs `node_modules`, which also exists only in the main checkout).

## 2. The problem

Every stage assumes the task is "build a new service from nothing":

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

## 4. What is done (step 1)

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

Nothing is committed. `git status` shows the full change set.

## 5. What remains

Build these in order; each is a usable checkpoint.

### Step 2: the task kind

- Add a `kind` to the requirements session state (`RequirementsState` in
  `backend/app/requirements/models.py`) and to `TaskSpec`. It starts unset.
- Add a tool the interviewer calls to record it, next to `record_requirement` in
  `backend/app/tools/requirements.py`. Copy the pattern of the language choice:
  the `set_language` tool in `backend/app/tools/api_design.py`, the "never assume"
  wording in `LANGUAGE_UNKNOWN` in `backend/app/api_design/prompt.py`, and the check in
  `design_problems` (`backend/app/api_design/openapi.py`) that blocks finalizing until it
  is set.
- Change the opening message and guidance in `backend/app/requirements/prompt.py` so the
  first question is the kind, and so the rest of the interview depends on it (a full
  interview for a new service; a short brief otherwise).
- Refuse to finalize a requirements session while the kind is unset (`validate_finalize`
  in `RequirementsWorkflow`).
- Show the kind on the requirements page.

Routing on the kind arrives with the steps below; in this step every kind still follows
the new-service path.

### Step 3: publish the contract into the repository

Today the OpenAPI document and requirements are deliberately kept out of the repository
(see the comment on `CONTRACTS_DIR` in `backend/app/agent_workflows/contracts.py`). Every
later kind needs them as its starting point.

- On publish (`publish_repository` in `backend/app/temporal/activities.py`), also write
  the contract to the repository, for example `contracts/openapi.yaml` and
  `contracts/requirements.json`.
- Make sure neither coding agent's workspace treats that directory as source: check
  `is_service_file` in `backend/app/implementation/activities.py` and the population of
  the test workspace in `backend/app/test_generation/activities.py`.
- The OpenAPI YAML is rendered from the structured API model. Steps 4 and 5 need the
  model back, so either store the model as JSON beside the YAML or write a parser. Storing
  the model is simpler and lossless.
- Repositories published before this step have no contract and are not supported
  (decision 2). Report a clear error when it is missing.

### Step 4: the bugfix path

The largest step. A non-API task must reach delivery without an API design, so the
requirements session has to be able to start a `DeliveryWorkflow` itself, with the
contract read from the repository checkout instead of from an API design session.

- `TaskSpec` needs the brief (problem, expected behaviour, acceptance criteria).
- `DeliveryWorkflow` branches on the kind. For a bugfix: no code generation; the test
  agent first, then the baseline run, then the implementation agent, then the final run.
- **Building the baseline image.** Verification takes a service image. The baseline needs
  one built from the unmodified checkout, which no stage produces today. Add that.
- **Telling new tests from old.** The baseline rule ("new fail, old pass") needs the list
  of tests the agent added. Compare test names before and after test generation, or have
  the stage report them. `VerificationResult` in `backend/app/verification/models.py`
  already lists failures by class and test name.
- **Prompts.** Both agents get a variant for this kind, in the style of the existing
  `REPOSITORY_PROMPT` and `REPAIR_PROMPT` in `implementation/agent.py` and
  `test_generation/agent.py`. The test agent: add tests for the brief, keep the rest of
  the suite untouched. The implementation agent: fix the described defect with targeted
  edits; do not regenerate anything.
- **Skipped code generation.** The implementation workspace currently has generated code
  copied over the repository (`prepare_inputs` in `implementation/activities.py`). For a
  bugfix it must be the checkout alone.
- **Reporting.** `DeliveryProgress` needs the baseline result and a distinct outcome for
  "not reproduced". The frontend needs somewhere to show a delivery that has no API
  design session: today delivery progress is rendered only inside `ApiDesign.tsx`, so
  extract that part into its own component first.

### Step 5: the API change path

- Start the API design session from the stored contract instead of `empty_api()`
  (`ApiDesignState` in `backend/app/api_design/models.py`, `start_session` in
  `backend/app/api_design/service.py`), and tell the designer it is editing an existing
  API.
- The rest already works: the repository mode of delivery keeps existing files and both
  agents are told to extend what is there. Check that the test agent keeps existing tests
  the new contract still supports (its `REPOSITORY_PROMPT` already says so).

### Step 6: the docs-only path

- A coding agent whose workspace and output are limited to documentation files. Follow
  "Add a future stage" in `docs/service-implementation.md`.
- Give verification a strategy per kind. For docs it returns an explicit "not
  implemented" result without starting any container, so the real check can be added
  later without touching the workflow.
- Publish despite that result, and mark the publication as unverified (in the commit
  message and in `RepositoryPublication`), so the page can say so. Today `_publish` in
  `DeliveryWorkflow` refuses anything that did not pass verification; that rule must stay
  for every other kind.

## 6. Traps

- **Do not let a session workflow finish while its delivery child is still running.** The
  first version started the delivery with `ParentClosePolicy.ABANDON` and returned. The
  Temporal time-skipping test server then stops advancing time, and every test that waits
  for the idle timeout hangs forever. This is why `ApiDesignWorkflow` waits for its
  delivery. The same applies when the requirements session starts one in step 4.
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
  `publication_branch` (`backend/app/github/checkout.py`), which strips the
  `api-design-session-` prefix. A delivery started by a requirements session has a
  different prefix.
- **Tests of the delivery pipeline live in `backend/tests/test_api_design_workflow.py`**
  and drive it through a design session with fake activities. When delivery gets a second
  entry point, move them to their own file with the fakes shared.
