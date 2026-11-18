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
default. The suite's other tests make 7. `tests/test_zz_rate_limit.py` runs last (pytest
runs modules in name order) and makes failed sign-ins until it gets 429 with
`Retry-After`, using up the budget for that minute. Keep new sign-ins out of modules
that sort after it. Wait a minute before running the suite again. The service must run
with the default `AUTH_LOGIN_ATTEMPTS_PER_MINUTE` of 10.

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
- Token tests decode the issued JWT (HS256, NFR-3) and check that a changed signature, a
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
- `GET /workflows` filters `repository`, `branch` and `onboarding_id`
  (FR-71 to FR-74, `tests/test_workflow_filters.py`): without Temporal the suite can only
  check that malformed values answer 422 with a `detail` (repository not `owner/name`,
  empty branch, onboarding ID not `onboarding-session-` plus 32 hex digits), assuming
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
- Architecture, API design and delivery stages are children of the task flow, not of the
  requirements session (FR-125, FR-127). `test_workflow_listing_accepts_the_architecture_type`
  in `test_architecture.py` still asserts a `requirements-session-` parent when the listing
  answers 200; against a service without Temporal (the supported target) it only observes the
  503, so it is kept unchanged. A service with Temporal would have to be checked against
  `parent_workflow_id` being the task flow's ID.
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
