# Plan: a browser route for criteria that only a laid-out page shows

Written on 2026-11-20. It assumes you know `plans/part-component-contracts.md` (parts,
part languages, the composite component suite) and the delivery pipeline as
`docs/service-implementation.md` describes it.

## 1. The problem

A bugfix of the requirement popup was routed, and all three of its criteria were routed
`unobservable`, so the delivery stopped:

- **FR-171**: the popup of a requirement ID is cut off by the scrolling panel it is
  drawn in.
- **FR-172**: the popup is shown in full, on top of the history column, inside the
  window.
- **FR-173**: the popup beside the history column is fully visible at desktop width by
  mouse and at a narrow width by tap.

The routing's reasons were correct. The contract tests reach only the HTTP API. A
`component` route of the `frontend` part runs in jsdom, which does no layout: an element
cut off by an `overflow` ancestor and a visible one look the same there. A helper that
computes a position could be tested, but it would only check a rule the drawn popup
might not follow.

The stop message then asked the user to "rephrase each of them as behaviour the
service's HTTP API shows". No such wording exists: the defect is in the web client's CSS
(`.requirement-popup` in `frontend/src/styles.css` is `position: absolute` inside a
scrolling panel), and the API is already right (FR-121, `lookup_requirements`). So every
criterion about layout, clipping, stacking, overflow or responsiveness is a dead end
today, and someone has to fix it outside the platform each time.

Such criteria *are* observable without judgement, by a real browser:

- *Not cut off, on top*: `document.elementFromPoint` at the popup's corners and centre
  returns the popup or a descendant of it. Where an `overflow` ancestor clips it, or
  another element covers it, it returns something else.
- *Inside the window*: the popup's `getBoundingClientRect()` lies within
  `0..innerWidth` and `0..innerHeight`.
- *Desktop by mouse, narrow by tap*: the same test at 1280×800 with `hover()` and at
  390×844 with `hasTouch`, `isMobile` and `tap()`.

## 2. The idea

Add a third test kind beside `contract` and `component`: **`browser`**, end-to-end tests
of a part's built web client in headless Chromium, driven by Playwright, with the API
answered by fixtures checked against `contracts/openapi.yaml`. A part language opts in
the way it opts into component contracts: TypeScript parts that build a web page with
Vite first. The window sizes and input are not fixed by the platform: the routing agent
decides them per criterion (3.2), so a bug only in the mobile view or only in the desktop
view is tested where it happens.

Playwright is pinned at the latest release, **1.65.1** (13 Nov 2026), in both places
that must agree:

- the npm package `@playwright/test@1.65.1`, exact (no range), in the suite's
  `package.json` and lock file;
- the runner image
  `mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579`
  (the multi-arch index digest), whose bundled Chromium is the one that package expects.

The template's README records both. A check in the backend tests fails when the package
version and the image tag differ. Upgrading is a deliberate change of both, followed by
the stability run of section 6.

**Decided: the image is pulled ahead of use on the workers**, so no delivery waits for
a 1.5 GB pull and a registry outage does not fail a run (3.8).

Everything else stays as it is. The baseline run proves the new tests fail on the
repository, the fix makes them pass, the review rounds and the repair loop work on the
reports, and the tests are pushed with the pull request. As with component tests, the
author writes from a contract, not from the code: a **page contract** plays the role the
stubs play for a seam.

Order of preference in routing: `contract` > `component` > `browser` > `unobservable`.
A criterion is `browser` only when the page's layout, painting, focus or input events
decide it, not when a function of the part could show it in jsdom.

## 3. Design

### 3.1 Route

- `Route = Literal['contract', 'component', 'browser', 'unobservable']` in
  `app/routing/models.py`. A `browser` route names its part and a **page**:
  `{requirement_id, route: 'browser', reason, page: {part, entry, viewports, roles, added?}}`.
  - `entry`: the path within the client the test opens (`/`, `/requirements/…`).
  - `viewports`: the names of the viewports of the contract (3.2) the criterion must
    hold in, at least one. The agent chooses them from the criterion: only `narrow` for
    a bug of the mobile view, only `desktop` for one of the desktop view, both when the
    criterion names both. It may add a viewport where the criterion must *keep*
    holding as a guard against the fix moving the bug, and its `reason` says why each
    one is there.
  - `roles`: the elements the test finds, as ARIA role and accessible name
    (`{role: 'tooltip'}`, `{role: 'button', name: 'FR-1'}`), or as a `data-testid`.
  - `added`: those of `roles` the page lacks yet (3.5).
- `read_routes` checks `page` like `read_seam` checks `seam`: a part the input named, a
  part whose language has a browser suite, an `entry` starting with `/`, a non-empty
  `viewports` list of names, and a non-empty `roles` list of valid entries.
- `judge_routes` is unchanged; `browser` counts as observable.

### 3.2 The page contract

Written by the routing agent in `parts/P/browser/`, published to
`contracts/parts/P/browser/`, kept in `routing/parts/P/browser/`, like the stubs of a
seam.

- `pages.json`: per page, its `entry`, the elements it declares (role, name, testid) and
  the API operations (by `operationId`) the page calls on load and on the declared
  actions.
- `fixtures/<operationId>[.<case>].json`: `{status, body}` responses the tests may
  answer them with.
- `viewports.json`: the named viewports the suite runs in, written by the routing
  agent: `{name: {width, height, input: 'mouse' | 'touch', scale?}}`. There is no
  default set. The agent defines what the criteria need, guided by the criterion's
  wording ("on a phone", "below 600 px", "on a wide screen") and by the client's own
  CSS breakpoints, which it reads in the checkout: a viewport on each side of the
  breakpoint the bug is tied to. The instructions suggest, not impose, common sizes
  (1280×800 mouse, 390×844 touch, 768×1024 touch). `touch` sets Playwright's
  `hasTouch` and `isMobile`. The host checks:
  - names: lowercase words;
  - width: 280–3840;
  - height: 400–2160;
  - `scale`: 1–3, default 1;
  - every viewport a route names exists;
  - every viewport is named by some route, so none runs for nothing.

  A published viewport may be reused but not changed; a different size is a new name.

Host checks (`app/routing/browser.py`):

- Every fixture body validates against the response schema of its operation and status
  in `contracts/openapi.yaml` (jsonschema, `$ref` resolved, no network). Thus the mocks
  cannot drift from the API the contract tests prove.
- Every operation named exists in the document.
- A published page, element, fixture or viewport may be extended but not changed or
  removed, as for published stubs.

**Conformance** (the counterpart of `check_conformance`): a trusted runner builds the
part (`npm ci --ignore-scripts`, `vite build`), serves `dist/` and opens each page at
each viewport with all API calls answered from the fixtures. It fails when:

- a declared element that is not `added` cannot be found once the page is idle;
- the page calls an operation that is neither declared nor answered by a fixture.

Errors read like stubtest's: `Page /: role=tooltip … is not present`. The routing agent
gets the same check in `/workspace/tools/` (`check-pages.mjs`), as it gets `check.mjs`
today.

### 3.3 The browser suite

A new `BrowserSuite` (a `ComponentSuite` with the same fields), template in
`app/languages/typescript/browser_suite/`:

- **Project:** `@playwright/test` and `typescript` pinned in its own `package.json` and
  lock file; tests in `tests/**/*.spec.ts`.
- **Configuration:** a trusted `playwright.config.mjs` sets up one Playwright project
  per viewport of `viewports.json`, with the reporter `junit` to `REPORT` and
  `CLASSNAME_PREFIX` honoured as in `vitest.config.mjs`.
- **Which viewports a test runs in:** each test carries one or more viewport tags
  (`test('…', { tag: ['@narrow'] }, …)`), and each viewport's project runs only the
  tests with its tag (`grep`). The author tags a test with the viewports its
  criterion's route names. The host reads the tags from the test file's tokens, as it
  reads test names. A test without a tag, with a tag no viewport has, or whose tag is
  not given as a string literal is untracked, so `validate_output` rejects it. The test
  review checks that the criterion's viewports are all covered. It fixes `reducedMotion: 'reduce'`,
  `deviceScaleFactor: 1`, `locale` and `timezoneId`. It sets `serviceWorkers: 'block'`
  and a `baseURL` of the static server.
- **Fixtures:** every request to the API's path prefix is answered from the contract's
  fixtures through a trusted `fixture(page, operationId, case?)` helper. Any request the
  test did not answer fails the test, naming the operation. There is no network.
- **Trusted helpers** (`tests/support/`, not written by the author):
  - `expectFullyVisible(locator)`: in the viewport, and on top at the 4 inset corners
    and the centre;
  - `expectInViewport(locator)`;
  - `reveal(locator)`: `hover()` in a mouse viewport, `tap()` in a touch one, so a
    criterion "by mouse and by tap" is one test run in both projects;
  - `settle(page)`: fonts loaded and no animation running.

  These are what make layout tests stable, and what the reviewer can rely on instead of
  reading geometry code.
- **Runner:** on the pinned `mcr.microsoft.com/playwright:v1.65.1-noble` image (by
  digest, section 2). Bundled fonts come from the
  image, never from the network. It installs the part and the suite with `npm ci
  --ignore-scripts`, builds the part, serves `dist/` on localhost with a SPA fallback,
  lists the suite with `playwright test --list` and runs `playwright test`. The whole run
  is under one 600-second timeout.
- **Test identity:** `tests/popup.spec.ts#desktop > the requirement popup > is fully
  visible`, with the viewport project first and then as for vitest. Its class name
  carries the viewport, so a failure says which window size failed. The same untracked
  test rule applies: names must be string literals, with no `each`/`for`.
- **Where tests live:** `P/browser-tests/` in the repository, and
  `repository-tests/browser/parts/P/` in the snapshot. `is_service_file` leaves out
  `browser-tests/` at any depth, like `component-tests/`.

### 3.4 Composition

The Playwright image cannot reasonably be copied into the service's runner, as Node is
for TypeScript component tests (`part_runtime`). So the browser suite is **not** a
member of the image the `CompositeSuite` builds. It is a second runner beside it:

- `build_runner` builds the browser runner of each routed part separately. Baseline,
  verification and the repair loop run it after the component runner, in the same
  isolated, network-less way, and merge its JUnit report under the class prefix
  `P~browser:`.
- `CompositeSuite.members` gains a member per browser part (`SuiteMember` with a `runner`
  of its own). `part_of` and `case_id` read the prefix, so the delivery page shows
  "frontend (browser)" beside "frontend" with its own counts.
- One author run still writes every project. Its instructions say that the tests of
  `browser` routes go in `P/browser-tests/`, from `contracts/parts/P/browser/` alone.

### 3.5 Added elements

A page element the client lacks, such as a `data-testid` or a role on a `span`, is
added before tests are written, through the existing seam step (`_prepare_seam`). It is
an `added` entry of a page, collected by `added_seams` under `P/page:<entry>`.
`SEAM_PROMPT` gets one sentence: an added element of a browser page is an attribute
(`role`, `aria-label`, `data-testid`) on an element that already exists, with no other
change. The conformance check of 3.2 then runs against the seam's output.

### 3.6 Routing and interview prompts

- `PARTS_PROMPT` (`app/routing/agent.py`): a part whose language has a browser suite
  also takes `browser` routes, for behaviour that only layout, painting, stacking,
  focus or real input events decide (cut off, covered, inside the window, at a window
  size, by hover or tap). Prefer `component` whenever a function of the part shows it.
  The `reason` says what the page shows and at which viewports. The agent defines
  the viewports itself (3.2): where the bug is tied to a window size, it reads the
  client's CSS breakpoints and names only the side the bug is on, plus the other side
  as a guard when the fix could move the bug.
- `language.json` gets `part_browser`: per part language, the page contract's
  instructions and the helpers the suite offers.
- The requirements interview (`PARTS_INTERVIEW` in `app/requirements/prompt.py`): when
  a criterion is about how a page looks or fits, record it in measurable form. Say which
  element, at which window sizes, by which input, and what must hold: inside the window,
  on top and not cut off, before or after another element. "Looks right" is not a
  criterion. This avoids the stop after the fact.
- `judge_routes`: the stop message no longer always says "the service's HTTP API". When
  the input had parts, it says "as behaviour the HTTP API, a seam of the code or a page of
  the web client shows". For a criterion whose reason is about layout, it suggests the
  measurable form above.

### 3.7 Review and publication

- The test review and the regression review get the helpers' source and the page
  contract. A browser test that asserts geometry without the helpers is a finding.
- `publish_repository` pushes `P/browser-tests/` and `contracts/parts/P/browser/`.
- The pull request's proofs name `frontend (browser): /` and the viewports.
- A **workflow patch** (`browser-tests`) gates the new route in `DeliveryWorkflow`.
  Deliveries started before it set `routing_status: unsupported` for a `browser` route
  and stop, as `COMPONENT_TESTS_PATCH` did, so their histories replay.

### 3.8 Pulling the runner image ahead of use

Every runner image and build happens in the Docker daemon the sandbox worker drives:
`sandbox` (Docker-in-Docker) in `docker-compose.coolify.yml`, whose images persist in
the `sandbox_data` volume, and the host's daemon in local development
(`backend/tools/dev_stack.py`). That daemon is where the image is pulled.

- **One list of pinned images.** `PREPULLED_IMAGES` in `app/docker_agents.py` holds
  references with digests, starting with the Playwright image of section 2. The
  browser suite's Dockerfile `FROM` is generated from the same constant, so the image
  that is pulled and the one that is built on cannot differ.
- **At worker start.** `app/agent_workflows/worker.py` `main()` starts
  `ensure_images(PREPULLED_IMAGES)` as a background task before the workers poll. It
  does not block startup, since the other stages do not need the image. For each
  reference it runs `docker image inspect` and then `docker pull` only when the image is
  missing, with a 15-minute bound and up to three attempts with backoff. It logs the
  start, the outcome and the time taken. Because of the volume, a redeploy finds the
  image and pulls nothing.
- **Before use.** The activities that build or run a browser runner (routing's page
  conformance, `build_runner`, baseline and verification) call `ensure_image` first,
  while the activity heartbeats (`keep_activity_alive`). Normally it finds the image and
  costs one inspect. If a fresh worker's background pull is still running, they wait
  for it, sharing the same task in the process, not a second pull. A pull that fails
  there is an infrastructure `error` of the stage, not a test failure, and the error
  names the image.
- **Builds use the local copy.** The runner build passes `--pull=false`. The build
  context references the image by digest, so a pulled copy is exact.
- **Keeping it.** Nothing in the platform prunes images today. Any later cleanup of the
  sandbox daemon must spare `PREPULLED_IMAGES`, and the operations docs say so.
- **Local development.** `dev_stack.py` runs the same `ensure_images` against the host
  daemon when it starts the sandbox worker.
- **Upgrading.** Changing the reference in `PREPULLED_IMAGES` makes the next worker
  start pull the new image. The old one stays until a person removes it, so a
  delivery still running on the old version is not broken mid-run.

### 3.9 Not in this plan

- **A `manual` route** for criteria no test can decide ("looks balanced"). The pull
  request would list them as checks for a person to tick, and the merge would wait for
  them. It is worth adding only if `browser` leaves real cases, and then the routing
  must say why `browser` could not show it. Decide after step 6.
- **Screenshot comparison or a vision model as a judge.** Neither is deterministic
  enough to make a test pass or fail. A screenshot may be attached to the delivery page
  as evidence later.
- **Browser tests against the running service.** The fixtures stand in for the API. The
  contract tests already prove the API, and the fixtures are checked against the same
  document.

## 4. Files to modify

- `backend/app/routing/models.py`: `browser` route, `read_page`, `added_seams` for
  pages, stop message.
- New `backend/app/routing/browser.py`: reads and checks the page contract, validates
  fixtures against the OpenAPI document, runs the conformance runner.
- `backend/app/routing/agent.py`, `activities.py`, `parts.py`: prompt, `part_browser`,
  tools, publishing the checked contract.
- `backend/app/languages/__init__.py`: `PartLanguage.browser_contract` and
  `browser_suite` (both optional), `BrowserSuite`.
- New `backend/app/languages/typescript/browser.py`: detection of a Vite web client,
  page contract instructions, suite definition.
- New `backend/app/languages/typescript/browser_contract/` (Dockerfile,
  `check-pages.mjs`) and `browser_suite/` (Dockerfile, `package.json`, lock file,
  `playwright.config.mjs`, `tests/support/`, `run-tests.sh`, README).
- `backend/app/languages/composite.py`, `backend/app/test_generation/components.py`,
  `backend/app/verification/`: a member with its own runner, report merge, `part_of`.
- `backend/app/delivery/flow/stages.py`: `browser-tests` patch.
- `backend/app/docker_agents.py`: `PREPULLED_IMAGES`, `ensure_image`, `ensure_images`.
- `backend/app/agent_workflows/worker.py`: background pull at start.
- `backend/tools/dev_stack.py`: the same pull for local development.
- `docker-compose.coolify.yml` comments and the operations docs: which images are
  pulled ahead and must not be pruned.
- `backend/app/implementation/agent.py` (`SEAM_PROMPT`), `activities.py`
  (`is_service_file`).
- `backend/app/github/checkout.py` and the publication: `browser-tests/`,
  `contracts/parts/P/browser/`.
- `backend/app/requirements/prompt.py`: `PARTS_INTERVIEW`.
- `frontend/src/Delivery.tsx`: the browser member's counts and routing line.
- `docs/service-implementation.md`: the route, contract, suite and runner.

## 5. Steps

- [x] 1. Browser suite template and trusted runner for TypeScript parts: config,
  helpers, fixture interception, JUnit with prefix. Prove it on this repository by hand
  with a test for FR-171–173 that fails on `main`.
  *Done.* Notes:
  - The proof: a page contract of the requirements interview
    (`/?mode=requirements&session_id=…`, answering `getAuthMe`, `getWorkflows` and
    `getRequirementsSessionsBySessionId`) and one test tagged `@desktop` and `@narrow`
    that reveals the `FR-1` button of the "Your answer" region and asserts
    `expectFullyVisible` on the tooltip. On `main` it fails at both viewports, saying why:
    at desktop "top right (471, 411) shows code; bottom right (471, 554) shows
    ol.conversation-list" (cut off and under the history column), at narrow "is not
    inside the window of 390×844". With the popup made `position: fixed` it passes, 40 of
    40 runs twice (`--repeat-each 20`).
  - The runner writes JUnit with a trusted reporter of its own (`reporter.mjs`), not
    Playwright's, so the class name is `file#viewport` after `CLASSNAME_PREFIX`.
  - A viewport's `scale` is its `deviceScaleFactor` (1 by default), so "fixes
    `deviceScaleFactor: 1`" holds unless the contract says otherwise.
  - `ComponentSuite.trusted` names the helpers, which `copy_test_suite` always puts
    over the author's copies, so the runner and the repository get the template's.
- [x] 1a. Pull the image ahead of use: `PREPULLED_IMAGES`, background pull at the sandbox
  worker's start and in `dev_stack.py`, `ensure_image` before every browser-runner
  build or run, and `--pull=false` builds.
  *Done.* Notes:
  - The constants live in `app/images.py`, which imports nothing, and
    `app/docker_agents.py` re-exports them: the languages build their Dockerfiles'
    `BASE_IMAGE` argument from them without importing the Docker machinery.
  - `dev_stack.py` starts the sandbox worker, which pulls at its own start against the
    host's daemon; it does not pull a second time.
  - `ImageUnavailable` is not a `RuntimeError`, so no caller mistakes it for a test
    failure or a contract mismatch; it fails the activity, naming the image.
- [x] 2. Page contract: format, host checks, OpenAPI fixture validation, conformance
  runner, and `check-pages.mjs` for the agent.
  *Done.* Notes:
  - `pages.json` also holds `api_prefix` (this repository's client calls the API under
    `/api`, which its dev server strips) and per page `actions`: a target, an input and
    the elements it shows. The popup exists only after hover or tap, so "found once the
    page is idle" could not cover it otherwise.
  - The fixtures are checked against the OpenAPI document rendered from the delivery's
    API design, or `contracts/openapi.yaml` without one; the routing keeps the API's
    operations in `routing/parts/P/operations.json` for the runners, since the
    repository the runners are built from holds no `contracts/`.
  - Conformance always runs at routing, with the route's `added` elements expected
    absent, rather than waiting for the seam as stubs with `added` symbols do.
  - The routing agent's `check-pages.mjs` needs a Chromium it may not be able to
    install; its instructions say so, and the host runs the same check regardless.
- [x] 3. Route: `read_routes`, prompt, `language.json`, publication of the checked
  contract; `browser-tests` patch.
  *Done.* Notes: the routing's result names `repository_parts`, which the workflow keeps
  in `DeliveryState.routing_parts` for the stop message.
- [x] 4. Composition: a separate runner member, baseline, verification and repair loop,
  report merge, delivery page.
  *Done.* Notes:
  - The workflow keeps one runner image per run, so its arguments do not change:
    `build_runner` records the runners beside it in
    `component-runners/<image>.json`, and `run_component_suite` runs them in turn and
    merges their reports. Its start-to-close timeout is 30 minutes instead of 15.
  - The browser member's tests live at `browser/P/` of the delivery's suite, and so the
    snapshot keeps them in `repository-tests/components/browser/P/`, not
    `repository-tests/browser/parts/P/`: the record is the suite's layout.
  - The workflow treats `browser` routes as component routes (`_component_routes`), so
    authoring, repair, diagnosis and review needed only prompts and paths.
- [x] 5. Added page elements through the seam step.
- [x] 6. Interview and stop message; review instructions; docs.
- [ ] 7. Re-run the popup bugfix (FR-171–173) through the platform end to end.

## 6. Verification

- Runner, with no LLM:
  - a fixture page whose popup is clipped by an `overflow: auto` parent fails
    `expectFullyVisible` at both viewports, and passes once it is portaled;
  - a request the test did not answer fails the test;
  - no network is reachable;
  - the 600-second bound holds.
- Host checks, table-driven: a fixture that breaks its schema, an unknown operation, a
  changed published page, a bad `entry`, an `added` element that exists already.
- Routing: `read_routes` accepts `browser` only for a part with a browser suite. A
  delivery recorded before the patch replays.
- Composition: either completion order of the runners. The class prefix tells
  `frontend` from `frontend~browser`. One failing viewport is reported by name.
- Viewports chosen by the agent: a fixture page whose popup is clipped only below a
  600 px breakpoint fails a test tagged `@narrow` and passes one tagged `@desktop`. A
  route naming only `narrow` runs no desktop project. A test without a tag, or with an
  unknown one, is rejected as untracked. An unused viewport, or a changed published
  one, is rejected by the host.
- Pinning: a backend test fails when the template's `@playwright/test` version differs
  from the runner image's tag.
- Pulling ahead, with a fake Docker transport:
  - a present image is inspected and not pulled;
  - a missing one is pulled once, even when the start-up task and an activity ask for
    it at the same time;
  - a failed pull is retried, then becomes a stage `error` naming the image;
  - worker start-up is not delayed by the pull.

  Opt-in real Docker: after the pull, build the browser runner with the registry
  unreachable and confirm it succeeds from the local copy.
- Stability: run the FR-171–173 tests 20 times on the fixed code; the run must not be
  flaky.
- End to end: the popup bugfix is routed `browser`, its tests fail in the baseline, the
  fix passes them, and the PR carries `frontend/browser-tests/` and
  `contracts/parts/frontend/browser/`.

## 7. Open questions

- **Run time.** A browser run adds roughly 1–2 minutes per viewport. Pulling the image
  is moved out of runs (3.8), but the `sandbox_data` volume needs about 1.5 GB more.
- **Detection.** Is a Vite client (`vite` in dependencies plus `index.html`) enough, or
  should `runtime.json` name the build command and output directory for other bundlers?
