# Plan: component contracts for changes the HTTP contract cannot observe

Written on 2026-11-02. It assumes you know the delivery pipeline as
`plans/task-kinds-handover.md` describes it.

## 1. The problem

A bugfix proves itself red → green with black-box HTTP contract tests (`contract-tests/`),
run against the service image built from the repository's root `Dockerfile`. That image
has no Temporal, no LLM and no GitHub. Any acceptance criterion whose behaviour needs one
of them to happen cannot be observed by that suite.

The first real case is the fix of flows closing when their pull request is merged or
closed. The criteria ("a merged pull request completes the delivery and its whole flow",
"no session of the completed flow can be forked", …) all need a flow with a pushed branch
and an open pull request, which the public API cannot create. What happened:

1. Nothing checked, before tests were written, whether the criteria were observable.
2. The test author is told never to skip a test, but has no other way to say "this
   cannot be tested". It skipped all 13 tests behind `CONTRACT_TESTS_MERGE_SESSION_ID`,
   a variable nothing sets.
3. `judge_baseline` (`backend/app/delivery/models.py`) correctly refused the skipped tests,
   but only after the interview was over, and with a message that reads as a fault in
   the tests. Every retry ends the same way.

The user must never have to say which suite proves a change. The flow decides.

## 2. The idea

Keep the separation the pipeline is built on: the test author never sees the
implementation, only a contract someone else wrote. For behaviour the HTTP contract
cannot reach, give it a **component contract** one level lower: the narrowest existing
interface of the code at which the behaviour is observable (a *seam*), written as stubs
with signatures, types and behaviour docstrings, without bodies.

- An agent **with** code access picks the seam and writes the stubs, the same pattern as
  onboarding, where an agent drafts the OpenAPI contract from existing code.
- The test author, **without** code access, writes in-process component tests against
  the stubs, with its own fakes for the collaborators the stubs name.
- The implementation author fixes the code and must keep the seam's signatures, as an
  API-preserving change keeps the API today. It does not see the component tests, as it
  does not see the contract tests.
- Verification checks that the code still conforms to the stubs, then runs both suites.

HTTP comes first. A criterion is routed to a seam only when the contract suite cannot
observe it, so tests are coupled to internals only where nothing else works.

For the flow-closing bug the seam is natural: Temporal workflows already have
contract-shaped interfaces. The stubs would be the delivery and session workflows'
signal or update that delivers "pull request closed (merged or not)", their `get_state`
queries and the fork entry points. The tests drive them in Temporal's in-process test
environment (`temporalio.testing.WorkflowEnvironment`) with activities the test author
fakes from their stubbed signatures. No GitHub, LLM or Temporal server is needed.

## 3. Design

### 3.1 Routing stage (new): `ObservabilityWorkflow`

Runs for `bugfix` (and later `api_change`) after the interview, before any test is
written. It is a coding agent with read-only access to the checkout and its
`contract-tests/`, and it is told how the contract suite's environment is built (the root
`Dockerfile`, `contract-tests/README.md`).

For each requirement of the brief it records one route:

| Route | Meaning |
|---|---|
| `contract` | Observable over HTTP against the service as verification builds it. |
| `component` | Observable only at a seam; names the seam (module and symbols). |
| `unobservable` | Neither. Carries the reason. |

Output: `routes: {requirement_id: {route, seam?, reason}}` and, when any route is
`component`, the stub files (section 3.2). It is stored in `DeliveryProgress.routing`.

If any route is `unobservable`, the delivery stops **here**, before tests are written,
with a message naming the criteria and the reason (for example "needs a real GitHub
pull request; no existing interface of the code shows it"). The user can rephrase the
criteria through the interview. This is the early, honest failure that was missing.

The routing agent may only pick a seam that **already exists** in the code as the
repository holds it. Otherwise the baseline would go red because of an `ImportError`,
which looks like a reproduced bug and proves nothing. A missing seam is `unobservable`
in the first version (see 3.8 for later).

### 3.2 The component contract

Stubs in the service's language, under `contracts/components/` of the repository, beside
`openapi.yaml`:

- Python: `.pyi` stub files mirroring module paths (`app/delivery/workflows.pyi`), with
  only the seam's symbols and the types they mention.
- Java (later): interface/abstract-class sources with Javadoc, without bodies.

Rules the routing stage's output is validated against (host side, like
`validate_output` in `test_generation/activities.py`):

- No function bodies other than `...` (Python: parse with `ast`; reject any statement
  but docstrings and `...`).
- Every stubbed symbol exists in the checkout (Python: `mypy.stubtest` against the
  baseline sources; any error rejects the output).
- Collaborators (activities, repositories, clients) appear only as their signatures, so
  the test author can fake them; their implementation never leaks through.
- Each docstring states behaviour the brief or the existing contract supports, not how
  the code does it. This one cannot be checked mechanically. The code review stage
  is told to check it.

Published stubs stay in the repository and are supplied to later routing runs, which
reuse or extend them. Changing a published stub is an API-level change of the seam and
is out of scope for a bugfix, like changing the HTTP API.

### 3.3 Test authoring

`generate_service_tests` gets the routes. For `contract` routes nothing changes. For
`component` routes, a second run of the test author (its own workspace, its own
`StageSpec`) writes `component-tests/`, a standalone project like `contract-tests/`:

- Its inputs are the brief, the routed requirements and the stubs. No service source.
- New prompt `COMPONENT_PROMPT` in `test_generation/agent.py`: test through the stubbed
  symbols only, fake every collaborator the stubs name, never fake the seam itself, one
  test per routed criterion, no skips.
- Both prompts gain a structured way out: the author may return
  `unobservable: [{requirement_id, reason}]` instead of writing a test. The delivery then
  stops as in 3.1. This replaces the skip-behind-an-env-var escape, and the "never skip"
  rule stays.
- The repository's own `component-tests/`, when they exist, are snapshotted and frozen
  exactly like `contract-tests/` (`snapshot_repository_tests`, `frozen` in
  `generate_service_tests`).

`TestSuite` in `app/languages/` gets a sibling `ComponentSuite` (template, instructions,
published files, test glob, `test_cases`, `case_id`), so test identity and protection
work the same way.

### 3.4 Running component tests

The runner is trusted and comes from the language template, never from the test author,
as `RUNNER_FILES` do today. For Python:

- `component-tests/Dockerfile` (template): starts from the service's build stage,
  copies the service sources **at build time** (the author never had them), installs
  the service's dependencies plus `pytest` and `temporalio[testing]`, then copies the
  component tests. `run-tests.sh` runs pytest with the JUnit XML report, the same report
  contract `summarize_reports` already reads.
- No network and no service container: the tests are in-process. The verification
  activity gets a `run_component_suite` sibling of `run_verification` that runs one
  container and collects its reports.

`build_baseline` builds the component image against the repository's sources too, so the
baseline covers both suites.

### 3.5 Verification and judging

- `VerificationResult` stays per run. `DeliveryProgress` gets `component_baseline` and
  `component_verification` beside `baseline` and `verification`.
- Before running component tests, verification runs the stub conformance check
  (`stubtest`) against the sources under test. A failure is reported as "the fix changed
  the component contract" and goes to the implementation side.
- `judge_baseline` and `unproven_green` take both results. The rules are unchanged: every
  added test fails at baseline, every existing test passes, nothing skips, nothing is
  missing. A skip in either suite is still an error, but its message now names it as a
  routing failure ("a test skipped itself: the criterion was routed to a suite that
  cannot observe it") rather than a test fault.
- The final run must pass both suites.

### 3.6 Implementation, diagnosis, repair and review

- The implementation author gets the stubs as part of its contract: "keep every
  signature in `contracts/components/` exactly". `component-tests/` is added to the
  directories it never sees (`implementation/activities.py`, beside `test_dirs`).
- Diagnosis gets failures of both suites. Its `target` stays `service` or `tests`. A
  repair of `tests` names which suite, and only that suite's author runs again.
- The code review gets the stubs and both suites' diffs, plus the docstring rule from 3.2.

### 3.7 Publication and UI

- The push includes `contracts/components/` and `component-tests/`. The pull request
  body lists which criteria each suite proves.
- The delivery page shows the routing (one line per criterion with its route) and, when
  present, the component suite's baseline and final results next to the contract
  suite's. An `unobservable` stop shows its reasons and points back to the interview.
- `contract-tests/README.md` gets nothing new. `component-tests/README.md` (written by
  the author, as today) states the seam and what is faked.

### 3.8 Later: preparing a seam

When no seam exists, a first sub-change could add one with no behaviour change: the
implementation author adds the stubbed interface, verified by both suites' existing
tests passing unchanged. Only then is the bug reproduced through it. This is a second
red → green cycle and is left out of the first version. The routing stage reports
`unobservable` instead.

## 4. Steps

Each step lands with its own tests (`backend/tests/`, stage fakes in
`backend/tests/pipeline.py`).

1. **Honest early failure, no component suite yet.** The routing stage with the
   `contract` and `unobservable` routes only. The test author's structured
   `unobservable` result. The reworded skip message in `judge_baseline`. On its own this
   turns the flow-closing case into a clear stop before tests are written.
   *Done:* `app/routing/` (`ObservabilityWorkflow`), `DeliveryProgress.routing*`, the
   test author's `unobservable.json`, and the Routing section of the delivery page; see
   "Changing an existing service" in `docs/service-implementation.md`. The test author
   does not get the routes yet (3.3).
2. **Component contract.** The `component` route, stub output, and its validation (no
   bodies, `stubtest` against the checkout) for Python.
   *Done:* `ComponentContract` in `app/languages/` (Python's in
   `app/languages/python/components.py`, its runner in `component_contract/`),
   `app/routing/components.py`, and `routing_status: unsupported`, which stops the
   delivery until steps 3 and 4 land. Learned while checking it against
   `app.delivery.workflows`: stubtest compares a stubbed class's `__init__` even when the
   stub omits it, cannot compare `Literal` aliases (the runner does), and mypy prefers
   modules of its working directory to the stubs (the check runs elsewhere). Stubs must
   be self-contained: a type from an unstubbed module is stubbed too or written `Any`.
3. **Component tests.** `ComponentSuite` for Python, the template runner, the second
   test-author run with `COMPONENT_PROMPT`, snapshot and freeze of `component-tests/`.
   *Done:* `ComponentSuite` in `app/languages/` (Python's template in
   `app/languages/python/component_suite/`), `ComponentTestGenerationWorkflow` and
   `app/test_generation/components.py` on its own queue, `DeliveryProgress.component_tests*`,
   and the contract tests' author given its `routes`. The suite is checked by building its
   runner against the baseline sources, which collects it. `judge_routes` no longer stops
   at `unsupported` (only histories without the `component-tests` patch do); the delivery
   stops after the tests are written, on the baseline, until step 4 runs them. Already
   here from 3.6: `component-tests/` is never a service source (`is_service_file`).
   Open question answered: the runner fetches Temporal's test servers only when the code
   imports `temporalio`.
4. **Verification.** `run_component_suite`, the component baseline, the conformance
   check, both results in `judge_baseline`, `unproven_green` and `_publish`.
   *Done:* `run_component_suite` and `build_component_runner` (conformance check, then the
   runner built with the changed code) in `app/verification/activities.py`,
   `DeliveryProgress.component_baseline` and `component_verification`, and `SuiteRun` for
   `judge_baseline`, behind the `component-runs` workflow patch. A change with no
   `contract` route runs the repository's own contract tests as they are, at the baseline
   and with the fix, or none when the repository has none. Until steps 5 and 6 land, a
   change with component tests is not repaired, reviewed or pushed: a fix that breaks the
   component contract ends it with `implementation_error` (retried by writing the fix
   again), a failing test with `verification_error`, and a pass with `publication_error`
   saying it cannot be pushed yet (`COMPONENT_CHANGE_UNREPAIRED`,
   `COMPONENT_CHANGE_UNPUBLISHED`).
5. **Implementation, diagnosis, repair, review** integration (3.6).
   *Done:* behind the `component-repairs` workflow patch. The implementation author gets
   `component_routes` and the routing's stubs in `contracts/components/`. The diagnosis
   sees both runs, and a verdict against the tests names their `suite` (`Diagnosis.suite`,
   `RepairRound.suite`). `generate_component_tests` takes a `repair`, and only the named
   suite's author runs. The code review gets `component-tests/` and
   `contracts/components/` as changes. Its findings go to the suite whose files they name
   (`for_suite`). Findings about the stubs go to no author (`correctable`) and stay open.
   A change that passed and was reviewed still stops before the push
   (`COMPONENT_CHANGE_UNPUSHED`). Learned while doing it: a contract suite that is the
   repository's own, run because no criterion was routed `contract`, has no author to
   repair it. All of its tests passed without the fix, so the diagnosis may not blame it.
   Mention and instruction fix rounds need a push, so they wait for step 6.
6. **Publication and UI** (3.7).
7. **End to end.** Run the flow-closing bugfix through the real pipeline with real
   agents and Docker. It is the acceptance case for the whole plan: routes go to
   `component`, the stubs are the workflows' signal or update and queries, the component
   tests fail at baseline and pass after the fix.
8. **Java** `ComponentSuite` and conformance check (compile the stubs against the
   service's classes, then compare signatures with `javap`).
9. **Seam preparation** (3.8), if step 7 shows it is needed often.

## 5. Open questions

- Is `api_change` routed too? Its new behaviour is HTTP by definition, but its side
  effects may not be. Start with `bugfix` only.
- Should a published seam be protected across later deliveries like contract tests are,
  or regenerated by each routing run? The plan above protects it. That is safer, but
  it adds a "change the seam" task kind sooner or later.
- How large may a stub be before it leaks the design? A limit on symbols per seam, or
  leave that to the code review?
- Should Temporal's test environment be part of the Python component template for every
  project, or only added when a stub imports `temporalio`?
