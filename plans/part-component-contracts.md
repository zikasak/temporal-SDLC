# Plan: component contracts for the other parts of a repository

Written on 2026-11-04. It assumes you know `plans/component-contracts.md`, whose steps
1 to 9 are done, and the delivery pipeline as `plans/task-kinds-handover.md` describes it.

## 1. The problem

The bugfix that closes flows when their pull request is merged or closed was routed, and
15 of its 17 criteria went to seams of the Python service (`app.delivery.workflows`,
`app.github.webhooks`, …). The other two were routed `unobservable`, and the delivery
stopped:

- **FR-115**: which flows the flow list highlights as waiting, and which get only an
  "unresolved findings" marker. This is decided by the web client (`frontend/src/Flows.tsx`,
  `sessionAttention`), not by the API.
- **TR-6**: the GitHub App manifest subscribes to `pull_request` events. The manifest is
  built and sent to GitHub by the web client (`frontend/src/github.ts`,
  `createGitHubApp`), and no endpoint exposes it.

The routing's reasons were correct for what a delivery can test today: the HTTP contract
tests reach only the API, and a component contract covers only the **service's** code, in
the session's one language (`contracts/language.json`). But both criteria can be
observed one level lower, in the web client's own code, with an in-process test:

- TR-6: call `createGitHubApp` in jsdom with `HTMLFormElement.prototype.submit` faked, read
  the hidden `manifest` input and check `default_events`. The seam already exists and
  is exported.
- FR-115: call `sessionAttention(flow, body)` with a closed flow whose delivery has
  `open_findings: true` and check what it returns. The function exists but is not
  exported, so the change exports it first (`seam.added`, step 9 of the earlier plan).

A bugfix's criteria may touch any project in the repository, not only the service. The
user must not have to rephrase them into API behaviour when a test of the project that
shows the behaviour can be written.

## 2. The idea

A repository holds **parts**: the service, in the session's language, and other projects
beside it, such as `frontend/` (TypeScript, React). `repository_parts`
(`app/agent_workflows/parts.py`) already finds them from their build manifests, and
`checkout_parts` gives them to the coding agents.

Let a `component` route name the part its seam is in. Each part language that has a
component contract (TypeScript first) gets the same three pieces the service languages
have: a component contract (stubs and a conformance check), a component suite (template,
trusted runner, test identity), and the instructions its routing and test author are given.

Everything after the routing should keep seeing **one** component suite, as it does
today. The suite becomes a composite of one sub-suite per part with routed criteria: one
author run writes all of them, one runner image runs all of them and leaves one JUnit
report per part, and test identities carry the part. Then `DeliveryWorkflow`, diagnosis,
repair, review and the delivery page need almost no change, and no workflow patch is
needed for anything but the publication (section 3.7).

## 3. Design

### 3.1 Parts and part languages

- `PartLanguage` in `app/languages/` (a registry beside `Language`): `name`, `label`,
  `detect(part_dir) -> bool`, `build_dirs`, `component_contract`, `component_suite`.
  TypeScript is the first: a part with `package.json` and `tsconfig.json` whose
  dependencies hold `typescript`. A service language is not a part language; the service
  part keeps its `Language`.
- `component_parts(language, checkout) -> list[ComponentPart]`: the service part (name
  `service`, root `.`, the session's language) and every other part of `repository_parts`
  that a registered part language detects, by name, which is its path (`frontend`). Parts
  nested inside the service's sources, and parts without a part language, are left out.
- Paths of a part `P` other than the service:
  - its published stubs: `contracts/parts/P/components/` (the service's stay in
    `contracts/components/`, so published contracts keep their place);
  - its component tests: `P/component-tests/`, a project of its own inside the part, as
    `contract-tests/` is at the root;
  - its snapshot: `repository-tests/components/parts/P/`, the composite suite's layout
    (3.5).
- `is_service_file` (`app/implementation/activities.py`) excludes `component-tests` at
  any depth inside a part, not only at the root, and `contracts/parts/`. The fix may
  change the part's sources (the web client) as it may the service's: nothing changes
  there.

### 3.2 Routing

- `prepare_routing` writes `parts` into the input (name, root, language label) and the
  instructions of each part language's component contract into `language.json`
  (`components` becomes the service's; `part_components` maps part language names to
  theirs). The published contracts of every part are copied to
  `/workspace/routing/components/` and `/workspace/routing/parts/P/components/`.
- `COMPONENT_PROMPT` (`app/routing/agent.py`) says that the repository may hold other
  parts beside the service, such as a web client, that a criterion about the behaviour of
  such a part is `component` at a seam of that part when its code can show it in-process,
  and that HTTP is still preferred where the API shows it. `PROMPT`'s "an outside system
  the service depends on" stays as it is: a web client is not one, it is code of the
  repository.
- `Seam` gains `part: NotRequired[str]` (absent for the service). `read_seam` reads it,
  and `read_routes` rejects a part the routing input did not name.
- `publish_components` reads and checks the contract of each part with routes, with
  that part's `ComponentContract` and root (`read_components` and `check_seams` per part,
  `check_conformance` per part), and keeps it beside the routing:
  `routing/components/` and `routing/parts/P/components/`. `routes.json` gains `parts`,
  the names and languages of the parts with routes, so later activities read them from the
  session's artifacts and no workflow argument changes.
- `added_seams` keys by `(part, module)`; `_prepare_seam` passes the result on unchanged,
  and `prepare_seam` checks every part's contract against the code with the seam.

### 3.3 The TypeScript component contract

`app/languages/typescript/components.py`, in the style of Java's
(`app/languages/java/components.py`):

- **Stubs** are `.d.ts` files at the module's path within the part
  (`src/github.d.ts` describes `src/github.ts`); the module is that path without suffix.
  A declaration file cannot hold an implementation, which `tsc` enforces in the
  conformance check. On the host, `read_stub` runs a small tokenizer over the source (like
  Java's `_tokens`, with strings, template literals, comments and brackets) and accepts
  only `export declare function|const|class|interface|type|enum` statements, `import
  type` and JSDoc comments. It returns the exported symbols, and members of exported
  classes and interfaces as `Class.member`. `statements` returns each top-level statement
  with layout and comments dropped, for the published-stub rule.
- **runtime.json**: `{"project": "frontend"}`, the part's root, checked to be a part the
  host found. The part's lock file and `package.json` are what the runners install.
- **Conformance** (`component_contract/`, a trusted runner on `node:24-slim`): the part
  installed with `npm ci` during the build, then `check.mjs` (TypeScript's compiler
  API) generates one check file per stubbed module and type-checks it with the part's
  `tsconfig.json` and `noEmit`. For each exported value `x` of the stub, the real export
  and the stub's must be assignable both ways
  (`const a: typeof Stub.x = Real.x; const b: typeof Real.x = Stub.x`). For each exported
  type, a mutual-assignability assertion is made. A missing export is an error naming it,
  as stubtest names missing symbols.
- Like Python's, the check runs without a network; only its build installs anything.
- **Tools for the routing agent**: `check.mjs` in `/workspace/tools/`, runnable as
  `node tools/check.mjs <project> <components> <module>…` after `npm ci` in the part.

### 3.4 The TypeScript component suite

`app/languages/typescript/component_suite/`:

- Template: `package.json` and `package-lock.json` with pinned `vitest`, `jsdom` and
  `@testing-library/react`, a trusted `vitest.config.ts` that resolves `@part/` to the
  part's root and sets `environment: 'jsdom'`, and `README.md`. The author writes
  `tests/**/*.test.ts(x)` and imports the part's code through `@part/`, only the
  stubbed modules.
- Runner: built on `node:24-slim` from the part's sources under `repository/` (installed
  with `npm ci`) and the suite under `suite/` (installed with `npm ci` from its own lock
  file; the author may add dev dependencies, as a Python author adds requirements). The
  build runs `vitest list` to collect the suite. A run is
  `vitest run --reporter=junit` without a network, its report written where the composite runner says (3.5).
- Identity: `test_cases` tokenizes each test file (the same tokenizer as the stubs) and
  takes `test(`/`it(` calls with a literal name, nested in `describe(` calls with literal
  names, as `tests/x.test.ts#Outer > inner`. Its source is the call's text without
  layout and comments. A test whose name is not a string literal is rejected by
  `validate_output`, as it could not be tracked. `case_id(classname, name)` matches the
  JUnit report vitest writes (`classname` is the file, `name` the describe path joined by
  ` > `). The tokenizer's results are checked against `vitest list --json` in the opt-in
  Docker tests.

### 3.5 The composite suite

`component_suite(language, parts) -> ComponentSuite` builds the suite of a delivery's
parts with routes. With the service alone it returns `language.component_suite`, as
today, so nothing changes for a delivery without other parts.

- **Layout** of the author's workspace and of the published artifact
  `component-tests/suite/`: the service's suite at the root, as today, and part `P`'s
  under `parts/P/`. Only parts with routed criteria are present, plus parts whose
  repository suite exists (their tests stay frozen).
- **Identity**: the service's cases as today; part `P`'s as `P:` + its case. `case_id`
  dispatches on the report's `classname`, which the runner prefixes the same way.
- **Runner**: one image. A generated Dockerfile with a stage per part, each the part
  suite's own Dockerfile, and a final stage on the service suite's image (or `node:24-slim`
  when no service criterion is routed) that copies each part's installed tree and Node.
  `run-tests.sh` runs every part's command in turn, each writing its own
  report into the service language's `test_reports` directory under a name its `pattern`
  matches (`junit-P.xml` for Python, `TEST-P.xml` for Java), and exits non-zero if any
  part failed. A part's runner that fails to build fails the image build, naming the part.
- `generate_component_tests`, `build_runner`, `build_component_runner`,
  `run_component_suite`, `snapshot_repository_tests` and the diagnosis call
  `component_suite(...)` with the parts read from `routing/routes.json`, instead of
  `language.component_suite`. Their signatures, and so the workflow's commands, do not
  change.
- The **author** gets the stubs of every part (`contracts/components/` and
  `contracts/parts/P/components/`), each part language's suite instructions and its
  tools. The agent image already has Python and Node.
- `build_component_runner` runs the conformance check of each part before it builds the
  runner, and returns the first `conformance_error`, naming the part.

### 3.6 Implementation, diagnosis, repair and review

- The implementation author's `component_routes` already carry the seam, now with its
  part; the prompt says to keep every stub of every part (`contracts/parts/`).
  `SEAM_PROMPT` says an added symbol of a TypeScript part is an `export` added to an
  existing declaration, or a new exported function, with no behaviour change.
- Diagnosis and repair name the `component` suite as today. The broken tests they name
  carry the part prefix, so `frozen_tests` and a repair of one part leave the others as
  they are.
- The code review gets each part's `component-tests/` and stubs as changes; `for_suite`
  sends findings in `P/component-tests/` to the component author.

### 3.7 Publication and UI

- `publish_repository` pushes `contracts/parts/P/components/` and `P/component-tests/` for
  every part with routes, behind a `part-component-pushes` workflow patch only if the
  workflow passes anything new; otherwise the activity reads the parts from the routing
  and needs no patch.
- The pull request's `proofs` list each criterion's part with its seam
  (`frontend: src/github.ts createGitHubApp`).
- The delivery page groups the component runs' tests by part, and the routing line of a
  criterion names its part.

## 4. Steps

Each step lands with its own tests (`backend/tests/`, stage fakes in
`backend/tests/pipeline.py`), and updates "Changing an existing service" in
`docs/service-implementation.md`.

1. **Parts and the TypeScript component contract.** `PartLanguage`, `component_parts`,
   `Seam.part`, the routing's inputs, prompt and per-part validation, the `.d.ts` reader
   and the TypeScript conformance runner. Until step 3 lands, the activity turns a
   `component` route with a part into `unobservable`, with a reason saying component tests
   of that part are not written yet, so no workflow code changes and the delivery stops as
   it does today, but with the right reason. Check the reader against the stubs of
   `createGitHubApp` and `sessionAttention` (as `added`) of this repository.
   *Done.* Notes for later steps:
   - `added_seams` still keys by module alone, and `routes.json` has no `parts` yet. Both
     are only needed once part routes reach the delivery, so they move to step 3, together
     with dropping `defer_part_routes`.
   - `check.mjs` copies each stub beside its module as a `.ts` file, not a `.d.ts` one.
     That way the part's `skipLibCheck` cannot skip it, relative imports resolve as the
     module's do, and types without `export` stay local to the stub. Interfaces and type
     aliases are compared whole. Classes are compared member by member, so a class stub
     may hold only the seam's members. JSDoc is part of a stub's statements, as Java's
     Javadoc and Python's docstrings are, so changing a published stub's documentation
     counts as changing it.
   - The routing agent installs a part in `/tmp`, because its workspace under
     `/workspace/routing` is copied back as its output.
   - `tests/test_typescript_components.py` runs `check.mjs` against this repository's
     `frontend/` when `RUN_NODE_CONTRACT_TESTS=1`.
2. **The TypeScript component suite.** Template, runner, `test_cases` and `case_id`,
   checked by building the runner against this repository's `frontend/` with a sample
   suite for TR-6, red against the code as it is (no `pull_request` event) and green
   once the event is added.
   *Done.* Notes for later steps:
   - The template pins `vitest` 5.1.0, `jsdom` and `typescript` 5.9.3, not
     `@testing-library/react`. The trusted `vitest.config.mjs` resolves a test file's
     import of a package the part depends on (such as `react` or the JSX runtime) from
     the part, so tests and code share one React. A library the suite installs is not
     resolved this way, because vitest leaves `node_modules` to Node. A library that
     imports React itself, such as `@testing-library/react`, would therefore bring a
     second copy. Rendering components waits for a case that needs it.
   - The report path is the `REPORT` environment variable of the config (default
     `/tests/reports/junit.xml`), so the composite runner (step 3) sets it per part
     rather than changing the command.
   - `vitest list` leaves out skipped and todo tests, but the JUnit report includes them,
     named as `test_cases` names them. The opt-in Node tests compare both. A file that
     fails to load is reported as one case named after the file, so its tests show up
     as missing.
   - Tests that cannot be tracked (computed names, `each`/`for`, a file the tokenizer
     cannot read) are named by the new `TestSuite.untracked_tests`, which
     `validate_output` enforces. The tokenizer reads regular expressions and, in `.tsx`
     files, elements of JSX as single tokens.
   - `PartLanguage.component_suite` is now required. The suite is not given to an author
     or built by a delivery yet: `defer_part_routes` still applies until step 3.
   - `tests/test_typescript_suite.py` runs the template's vitest against this
     repository's `frontend/` when `RUN_NODE_CONTRACT_TESTS=1`.
     `tests/test_component_contract_docker.py` builds the runner image and runs TR-6 red,
     then green, when `RUN_DOCKER_VERIFICATION_TESTS=1`.
3. **The composite suite.** `component_suite(...)`, the author's workspace with parts, the
   composite runner, per-part conformance in `build_component_runner` and `prepare_seam`,
   snapshot and freeze of each part's `component-tests/`, the prefix in diagnosis and
   repair. Drop the step-1 downgrade.
4. **Publication and UI** (3.7).
5. **End to end.** Run the flow-closing bugfix again, with FR-115 and TR-6 as written:
   both routed to `component` in part `frontend` (FR-115 with `sessionAttention` in
   `added`), red at baseline, green with the fix, pushed with `frontend/component-tests/`.

## 5. Open questions

- Should parts be declared, for example as `parts` in `contracts/language.json`, rather
  than detected? Detection needs nothing from the user and `repository_parts` is already
  trusted by the coding agents; declaration would rule out a misdetected part. Start with
  detection, limited to registered part languages.
- One author for all parts, or one per part? One keeps the stage, its questions, its
  `unobservable` result and its repair as they are. One per part could run in parallel and
  give each a smaller workspace. Start with one.
- TR-6's last sentence ("storing the returned webhook secret encrypted is unchanged") is
  not a behaviour the change makes. It is better left out of the criterion than proved,
  and the interview could say so. That is the interviewer's job and out of scope here.
- FR-115 is about what the page shows. A test of `sessionAttention` proves which items a
  flow gets, not how they are drawn. The reason the routing records should say that, as
  the routing reason of FR-110 does for the API.
