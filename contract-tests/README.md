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
default. The suite makes 5; keep new tests within that budget or sign in once through
the shared fixtures.

Endpoints that need Temporal, an LLM or GitHub are covered only as far as the service
answers without them: authentication, input validation and unknown IDs.

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
