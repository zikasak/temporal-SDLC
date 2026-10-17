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

Endpoints that need Temporal, an LLM or GitHub are covered only as far as the service
answers without them: authentication, input validation and unknown IDs.

Contract ambiguities and how the suite reads them:

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
- `/start` answers 503 with a `detail`, because the service image has no Temporal (NFR-4).
- Pull requests (FR-34 to FR-39) and the manifest's `pull_requests: write` permission
  (TR-4) need a pushed delivery or GitHub, and no endpoint exposes the manifest. So the
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
