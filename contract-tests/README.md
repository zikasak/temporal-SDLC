# Service contract tests

This standalone pytest project tests the API over HTTP, as `contracts/` describes it.
It needs Python 3.12, or its built Docker image.

Start the service and wait until it is ready, then run:

```sh
pip install -r requirements.txt
SERVICE_BASE_URL=http://localhost:8080 python -m pytest
```

The Dockerfile at the repository root builds the service as the tests expect it: the API
with its own empty PostgreSQL database, on port 8080, without Temporal, an LLM provider
or GitHub.

```sh
docker build -t temporal-project-service .
docker run --rm -p 8080:8080 temporal-project-service
```

`SERVICE_BASE_URL` is required and must point at the running service. Tests fail when
the service is unavailable.

The suite registers the first account itself while registration is open. Against a
service that already has accounts, supply the first user's credentials as
`CONTRACT_TESTS_ADMIN_LOGIN` and `CONTRACT_TESTS_ADMIN_PASSWORD`.

The API allows 10 sign-in and registration attempts per client address per minute by
default. The modules before `tests/test_zy_session_renewal.py` make about 9 (one is the
non-administrator account of `tests/test_openai_endpoint.py`, one replaces the
administrator's session after `test_logout_while_signed_in_clears_the_cookie` signs it
out). `tests/test_zy_session_renewal.py` starts about 10 sessions of its own. Sign-ins
that set up test data go through `conftest.sign_in`, which waits once for `Retry-After`
when it gets 429, so the run may pause for up to a minute. `tests/test_zz_rate_limit.py`
then makes failed sign-ins until it gets 429 with `Retry-After`, using up the budget for
that minute, and `tests/test_zz_refresh_rate_limit.py` does the same for `POST
/auth/refresh` (NFR-13, 30 per minute by default). Keep new sign-ins and refreshes out
of modules that sort after them. Wait a minute before running the suite again. The
service must run with the default `AUTH_LOGIN_ATTEMPTS_PER_MINUTE` of 10 and the default
refresh limit of 30. It must also run with the default access token lifetime of 15
minutes or longer, because the administrator's token is used for the whole run.

The webhook tests assume the connection has no webhook secret, so they run against a
service without a connected app. Raw-body signature verification (a signature over the
transmitted bytes passes, one over a re-serialised copy is rejected) is not tested: it
needs a connection with a known webhook secret, which the suite cannot create without a
real GitHub App, and no test may skip itself.

Endpoints that need Temporal, an LLM or GitHub are covered only as far as the service
answers without them: authentication, input validation and unknown IDs.

Contract ambiguities and how the suite reads them:

- The contract does not say whether an unknown session (404) is checked before the
  10000-character limit of findings instructions (413/422). The oversize test uses an
  unknown session, because a session with raised findings cannot be created without
  GitHub and Temporal, so it accepts 404, 413 or 422.

- NFR-1 does not say what form `Retry-After` takes. Either delay-seconds (0 or more) or
  an HTTP-date is accepted.
- FR-5 does not say how the cookie is cleared. Any `Set-Cookie` for `access_token` with
  an empty value, `Max-Age` of 0 or less, or an `Expires` date in the past is accepted.

- The contract says surrounding whitespace in a login is ignored only at sign-in (FR-2).
  When an account is created, a padded login may be rejected with 422 (FR-7). So the
  duplicate-login test sends the existing login in a different case without padding and
  expects 409.

- Code review (findings accept/instructions, the `review` retry stage) needs a finished
  delivery, which can't be built without Temporal, an LLM and GitHub. So the suite covers
  only sign-in, input validation (instructions of 1 to 10000 characters after trimming,
  `stage` from the documented enum) and unknown sessions (404).
- When the input is invalid, it is rejected (422, or 413 for too-long text) before
  the session is looked up, the way FastAPI validates requests. So invalid bodies are
  sent to unknown session IDs.
- Text over 10000 characters may answer 413 or 422 (NFR-5); both are accepted.
- `POST /connectors/github/events` (FR-44, FR-49) is covered only for rejected deliveries:
  the suite never connects a GitHub App, so the connection has no webhook secret and every
  delivery, unsigned, wrongly signed or signed with a guessed key, must answer 401. A
  correctly signed delivery (202/204), mention authorisation, classification, queueing
  and replies (FR-40 to FR-48, FR-51) need a real secret, GitHub and an LLM, so they
  aren't tested (`tests/test_github_webhook.py`). The suite also assumes that a
  rejected delivery does not use up its `X-GitHub-Delivery` ID (a repeat answers 401,
  not 409). The suite does not assert the order of 401 and 422 for a delivery with an
  invalid body, because the contract does not state it.
- Pull requests (FR-34 to FR-39) and the manifest's `pull_requests: write` permission
  (TR-5) need a pushed delivery or GitHub, and no endpoint exposes the manifest. So the
  suite checks only that the retry stages pass validation and reach the session lookup
  (404 for unknown sessions): `server`, `tests`, `documentation` and `repository` (which
  pushes again and retries the pull request) in
  `test_sessions_and_tokens.py::test_every_documented_stage_is_accepted_for_retry`, and
  `review` in
  `test_delivery_review.py::test_review_stage_retry_of_an_unknown_session_is_not_found`.
  Together these cover all five stages of `RetryStageInput.stage`.
- Token tests decode the issued JWT (HS256, NFR-14) and check that a changed signature, a
  changed `exp` or an unsigned (`alg: none`) token answers 401. The `exp` claim is the
  standard JWT claim. The suite checks only that `expires_at` lies in the future; it
  doesn't assume the configured TTL.
- `GET /workflows` without filters answers 503 with a `detail`, because there is no
  Temporal (NFR-4). An unknown `type` is rejected with 422 before Temporal is contacted.
- The PUT endpoint's "allowed origin, if any" depends on configuration the contract
  doesn't name, so it isn't tested. Neither is the number of answers per kind for a
  requirements session, nor the `400` for multipart chat requests, because the contract
  doesn't say what triggers them.
- Logins allow letters, digits and `. _ % + -`, 3 to 254 characters; passwords 8 to
  1024. The boundaries are tested with new unique accounts, because accounts can't be
  deleted through the API. Each run adds a few accounts.
- Browsing repositories/branches and checking the connection while no app is connected
  (`tests/test_access_and_unconfigured_github.py`): the contract does not say which
  refusal applies. For the repository listing only 409 and 503 are documented (besides
  401 and 422), so one of those is required; for the branch listing 404 is documented
  as well, so 404, 409 or 503 is accepted. Every refusal needs a `detail`, never 200.
  Connection check accepts 409 or 503. A JSON array as the body of check/disconnect is expected to be
  rejected with 422 (FR-50, NFR-6: "the body is an empty JSON object").
- Onboarding (FR-56 to FR-66, `tests/test_onboarding.py`) needs GitHub, Temporal and an
  LLM to get past the first step, so the suite covers sign-in, input validation (`decision` enum
  replace/decline/architecture_only, the former `replace_contract` body rejected, `message_index`, answers, instructions, stage enum, messages of 1 to
  10000 characters) and unknown onboardings (404) on every onboarding endpoint. Starting an
  onboarding on a repository the service cannot reach must be refused; the contract lists
  404, 409 and 503, and any of them is accepted. `GET /workflows?type=OnboardingWorkflow`
  must not be rejected as an unknown type: 200 (listing only onboardings) or 503 without
  Temporal are accepted. Contract decisions, forks, the one-unfinished-onboarding rule
  (409), existing artifacts, checkout updates and the pushed branch name
  `onboarding/<source branch>-<short id>` need a real repository and are not tested.
- `GET /workflows` filters `repository`, `branch` and `origin`
  (FR-71 to FR-74, FR-166, `tests/test_workflow_filters.py`): without Temporal the suite can only
  check that malformed values answer 422 with a `detail` (repository not `owner/name`,
  empty branch, origin other than `requirements` or `onboarding`). The former `onboarding_id`
  filter and field were removed (FR-164), so its 422 checks were replaced by `origin` ones; assuming
  this validation happens before Temporal is contacted, as the contract says "like an
  invalid type or page_token". Well formed filters must give 200 or 503. When Temporal
  is available and the list is not empty, the new fields (always present, null when
  not applicable) and the filter matching are checked on whatever executions come back;
  with no executions or no Temporal this part verifies nothing more. Matching of
  filters against real executions, the case-insensitive repository filter, and the
  onboarding IDs of DeliveryWorkflow/CodegenWorkflow executions need Temporal, a
  repository and an LLM, so they are not tested.
- Architecture sessions (FR-78 to FR-88, `tests/test_architecture.py`) are started only by a
  finalized requirements session, which needs GitHub, Temporal and an LLM. The suite covers
  sign-in, input validation (messages of 1 to 10000 characters, answers, `message_index`),
  unknown sessions (404) on every architecture endpoint, and that
  `GET /workflows?type=ArchitectureWorkflow` is a documented type (200 or 503). The contract
  does not say whether an invalid `format` of the document download (422) is checked before
  the session lookup (404), so both are accepted. Drafting, skip proposals, declining,
  finalizing, skipping and the 409 rules need a real session and are not tested.
  There is no `POST /architecture/sessions` or `POST /api-design/sessions`: both stages are
  started by the previous stage (FR-82); `test_api_design_sessions_cannot_be_started_directly`
  and `test_architecture_sessions_cannot_be_started_directly` check that the routes do not
  exist (404 or 405). The legacy `test_api_design_needs_an_existing_requirements_session` is a
  protected test that must stay unchanged; it only observes that this route answers 404 and
  does not claim any documented behaviour of the removed endpoint.
- Flow closing (FR-101 to FR-108, FR-110 to FR-113, FR-116) and requirement lookup
  (FR-117 to FR-124, `lookup_requirements`) happen on sessions that need GitHub, Temporal and an
  LLM, and the webhook that detects a merged or closed pull request needs the webhook secret.
  `tests/test_flow_closing_and_access.py` therefore checks only that an unsigned or wrongly
  signed `pull_request` delivery (merged or closed) is rejected with 401, that `top_level` of
  `GET /workflows` must be a boolean (422 otherwise; 200 or 503 for `true`/`false`), and
  that every requirements and API design session action requires sign-in. The contract does
  not say how an invalid `page_token` is reported before Temporal is contacted, so it is not
  tested.
- Task flows (FR-125 to FR-143, `tests/test_task_flows.py`): a `TaskWorkflow` is started by
  `POST /requirements/sessions` (or a fork, or a fix round), which needs GitHub, Temporal and
  an LLM, so task flow creation, pending/skipped children, naming, fork/fix flows and the
  `unresolved_findings` marker cannot be exercised. The suite checks that `TaskWorkflow` is a
  documented `type`, that `top_level`, `parent`, `type`, `repository`, `branch` and
  `onboarding_id` combine (200, or 503 without Temporal), that a malformed filter next to
  `parent` or `top_level` is still a 422, that `parent` with `top_level=true` is a 422, that
  the listing needs sign-in, and, only when Temporal does answer, that the executions carry
  `name`, `linked`, `waiting_for_user` and `unresolved_findings` and that task flows and stages
  are shaped as described. The contract does not name the format of a task flow's
  `workflow_id`, so the `parent` filter is tried with an arbitrary well-formed ID. It does
  not say whether `parent` with `top_level=false` is rejected, so that is not tested.
  The contract says a malformed `page_token` answers 422 but not whether this is detected
  before Temporal is contacted, so it is not tested.
- Unknown IDs are well formed (prefix and 32 hex digits) so that they are treated as
  unknown rather than malformed.

Collect the tests without contacting the service:

```sh
python -m pytest --collect-only -q
```

The Docker runner collects the suite during its build and executes it when started:

```sh
docker run --rm --network service-tests -e SERVICE_BASE_URL=http://service:8080 TEST_IMAGE_ID
```

The verification worker starts both images on one network, supplies
`SERVICE_WAIT_HOST=service` and `SERVICE_WAIT_PORT=8080`, and waits up to two minutes for
TCP readiness before running pytest, which is limited to ten minutes. The JUnit XML
report is written to `/tests/reports/junit.xml`.

## Session renewal with refresh tokens (FR-237 to FR-246, NFR-10 to NFR-14, TR-13 to TR-16)

Signing out now revokes the session on the server (FR-241, NFR-12). The `admin` fixture in
`conftest.py` therefore watches for `POST /auth/logout` and signs the administrator in again
afterwards. `test_logout_while_signed_in_clears_the_cookie` is kept unchanged. Tests that
revoke sessions use sessions of their own, signed in as an account that
`tests/test_zy_session_renewal.py` creates.

Covered:

- Sign-in returns `refresh_token` and sets the `refresh_token` cookie (HttpOnly,
  SameSite=Strict, `Path=/api/auth/refresh`). The first account's Session also carries one.
- Refresh by body and by cookie alone. The new access token works, and both tokens and
  cookies are replaced. A body token is used instead of the cookie, and an unusable bearer
  token is ignored.
- A just-replaced token is accepted again at once (grace period). After waiting 40
  seconds, reusing it answers 401 `revoked`, and the session's current refresh and access
  tokens stop working.
- A missing or unknown token answers 401 `invalid` with `WWW-Authenticate: Bearer`. A
  non-string token or a non-object body answers 422.
- Sign-out by bearer or cookie access token, or by refresh token in the body only, revokes
  the session: its access token gets 401 and its refresh token gets 401 `revoked`. Both
  cookies are cleared. When an access token is present, the session named in the body is
  left alone.
- The refresh rate limit is separate from the sign-in limit, and its 429 carries
  `Retry-After`.

Not testable over HTTP, so not tested:

- Idle (7 days) and absolute (30 days) limits, the `idle` and `expired` reasons, and last
  activity updates.
- Access token lifetime and settings documentation (TR-13, TR-16).
- The 503 without `AUTH_JWT_SECRET`.
- Rejection of tokens without a session id claim (TR-15). A valid signature can't be forged
  without the secret, and the claim's name is not specified.
- Web client behaviour (FR-243 to FR-245).

Contract readings:

- The rejection that reuses a token past the grace period is expected to carry reason
  `revoked`: the token is known, and the reason list names reuse under `revoked`.
- The contract marks the request body of refresh and logout as required but also says it
  is optional, so the suite always sends at least `{}`.
- The cookie's path is `/api/auth/refresh` (the web app's proxy prefix), so cookie tests
  send the `Cookie` header explicitly.
- Whether the access token from before a refresh keeps working is not specified, so it is
  not tested.

## Requirements in the database and their components (FR-247 to FR-265)

This version stores requirements in PostgreSQL per task flow and groups them by component. It
also adds `contracts/architecture.json` next to `architecture.md`, and gives every
`lookup_requirements` entry and `Requirement` a `component` (FR-265 replaces FR-120, FR-262 replaces
FR-85, FR-263 replaces FR-93). All of this can only be seen on existing sessions, or in commits a
delivery pushes. Those need GitHub, Temporal and an LLM, and the self-contained service image has
none of them. The read-requirements tool, the sync at flow start, the copies made by a fork,
component inference and the mapping done during conflict resolution are internal to the agents
and not exposed over HTTP. So these requirements are not tested.

The part this version touches that can be reached over HTTP is the architecture document's
`format=json` (architecture.json). The existing tests in `tests/test_architecture.py` already
cover it: sign-in, 404 for an unknown session with `md`, `json` and the default, and 404 or 422 for
any other format. No existing test contradicts the new contract, so none was changed or removed.

## Contract note: parent of an architecture stage

`WorkflowExecution.parent_workflow_id` says a stage of a task has the task flow as parent, so an
architecture workflow's parent is not a `requirements-session-*` ID.
`test_architecture_stage_parent_is_its_task_flow` asserts this. The older
`test_workflow_listing_accepts_the_architecture_type` is retained unchanged and still asserts the
superseded `requirements-session-` prefix. It only applies when the listing returns architecture
items, so it needs a service with Temporal.

## Agent questions and requirement lookup (FR-185 to FR-191, NFR-9, TR-8)

These requirements change the web client only; TR-8 says the API, including `agent_questions`
and the endpoint that answers them, stays unchanged. The popup, left-column form and layout
rules are not observable over HTTP. `lookup_requirements` (FR-119, FR-120, FR-191) and
`agent_questions` appear only in responses of existing sessions, which need GitHub, Temporal
and an LLM, so they are not tested. `tests/test_agent_answers.py` covers the answering endpoint
`POST .../delivery/answers` of requirements, API design and onboarding sessions: sign-in, the
`AgentAnswersInput` body (`workflow_id` string, `answers` list of strings; 422 otherwise,
assumed to be checked before the session lookup like other input) and 404 for unknown sessions.

## CI of the pull request's head commit (FR-3, FR-210 to FR-213)

The `ci` field (`CiChecks`: `commit`, `status`, `since`, `checks`, `fix_round`,
`pushed_outside`) appears only in the delivery progress of the requirements, API design and
onboarding session responses, and only for a delivery that has pushed and opened a pull request.
That needs GitHub, Temporal and an LLM, which the self-contained service image does not have, and
the head commit's author, the grace period and the timeout cannot be set up over HTTP. So these
requirements are not tested, and no test is written that would only check its own fixtures. The
existing tests are unaffected: this change adds a response field and no endpoint, parameter or
status code.

## Task kind recorded in the interview (FR-224 to FR-236)

- `GET /requirements/intake` was removed (FR-227). `test_intake_lists_every_kind_of_task`
  was deleted, and the sign-in check in `test_auth.py::test_endpoints_require_sign_in` uses
  `/auth/users` now instead of the intake. `tests/test_task_kind_in_interview.py` checks that
  the intake answers 404 to a signed-in user. The contract does not say what an anonymous
  request to the removed route answers, so that is not tested.
- `POST /requirements/sessions` takes only `repository` (`full_name`, `branch`) and `task`.
  Any other field, including the removed `kind` and `answers`, answers 422. Older tests that
  send `kind`/`answers` and expect 422 still hold under this rule, so they are kept unchanged.
  A body with just the three fields must pass validation. Without GitHub the service can't
  reach the repository, so 404, 409 or 503 (with a `detail`) is accepted, and 201 when it can.
  The suite reads 401 for an anonymous request as taking priority over body validation
  (FastAPI resolves the sign-in dependency first). Whether a blank task is rejected is not
  documented, so it is not tested.
- Proposing and recording the kind (`set_task_kind`), `RequirementsSession.kind`, finalize
  answering 409 while no kind is recorded, the children created at finalize per kind, idle
  sessions, the `skipped` status and task flow closing all need a running interview (GitHub,
  Temporal and an LLM), so they are not tested.

## Requirements review, settings and the `origin` filter

- `tests/test_requirements_review.py` (FR-144 to FR-147, FR-153, FR-157, FR-158, FR-168, FR-169):
  a review round runs only on a finalized session, which needs GitHub, Temporal and an LLM. The
  suite covers sign-in, `POST .../review/retry` of unknown requirements and onboarding sessions
  (404), the boolean `force` query parameter of `finalize` (422 for a non-boolean, assumed to be
  checked before the session lookup like other input; 404 for an unknown session when valid) and
  the `answers` body of requirements and API design sessions. Review rounds themselves,
  contradictions, `409` rules and `force` behaviour need the real dependencies and are not tested.
- `tests/test_settings.py` (FR-148): `GET /settings` is 200 or 503 (the contract documents 503 for
  an unavailable provider); on 200 the ten workflow types including `requirements_review` and the
  label `Requirements review` are checked. `PUT /settings` input validation (every type
  required, no unknown type, entry bounds) is asserted as 422 without assuming a provider. A
  successful save is not tested: it needs a model the providers offer, which the suite cannot know
  without an LLM provider, and it would change the shared settings.
- `tests/test_openai_endpoint.py` (FR-174 to FR-182, NFR-8, TR-7): the `openai_endpoint` section of
  `GET`/`PUT /settings`. Checked: section shape by role (administrator: `base_url`,
  `display_name`, `api_key_set`; another user: no `base_url` or `api_key_set`), no `api_key`
  field anywhere, `openai` listed in `providers` exactly while a base URL is saved, 403 for a
  non-administrator whose body contains the section (also `null`) with the stored endpoint
  unchanged, validation of the section (422: missing/non-http(s)/over-2048 base URL, display
  name over 100 characters after trimming, wrong types, non-object section), a changed base URL
  without a new key (422), an unreachable endpoint (422, nothing saved, key not echoed), removal
  of an unsaved endpoint as a no-op, and a non-administrator saving workflow settings without
  the section. The suite has no OpenAI-compatible server to point the service at (the service
  runs in another container), so a successful endpoint save, the display name as provider
  label, keeping the stored key on an empty field, the 409 removal conflict and the
  workflow-settings-roll-back-with-a-failed-check rule are not tested. Notes on how the
  contract is read:
  - Tests that must succeed re-send the workflows returned by `GET /settings`; because the
    contract documents 503 when a provider's models cannot be listed, `200` or `503` is
    accepted there (the 503 needs a `detail`).
  - The tests that look at an unsaved endpoint assume a service whose OpenAI endpoint has
    never been saved (a fresh database) and fail with a message otherwise.
  - The contract does not say in which order a non-administrator's 403 and other
    validation errors apply; the suite sends otherwise valid workflows with the section.
  - The non-administrator's `display_name` is read with `.get`, since the contract does not
    say whether a null name is omitted or sent as null.
  - `OPENAI_BASE_URL` and `OPENAI_API_KEY` (TR-7) are service configuration and are not
    observable over HTTP, so they are not tested.
- `tests/test_workflow_origin.py` (FR-149, FR-163, FR-164, FR-166, FR-167): `origin` filter and
  field, `RequirementsReviewWorkflow` type, no `onboarding_id`, stages never top-level, pending and
  skipped statuses. Everything except the 422 checks and sign-in needs Temporal, and without it
  verifies only the documented 503.
- Existing tests that mention `onboarding_id` or an architecture parent of `requirements-session-`
  (`test_executions_carry_the_new_fields_and_keep_the_old_ones`,
  `test_well_formed_filters_are_not_rejected`, `test_workflow_listing_accepts_the_architecture_type`
  and similar) only inspect executions when Temporal answers; they are kept unchanged, because
  without Temporal they observe nothing but the documented 503. Only the part of the suite that
  asserted the removed `onboarding_id` filter's 422 was replaced.
