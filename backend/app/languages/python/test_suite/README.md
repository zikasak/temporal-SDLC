# Service contract tests

This standalone pytest project tests the service over HTTP using requirements
and the public API contract. It needs Python 3.12, or its built Docker image.

Start the implemented service and wait until it is ready, then run:

```sh
pip install -r requirements.txt
SERVICE_BASE_URL=http://localhost:8080 python -m pytest
```

`SERVICE_BASE_URL` is required and must point at the running service. Supply credentials through
environment variables when the API requires authentication; document their names
here when adding those tests. Tests must fail when the service is unavailable.

Collect the tests without contacting the service:

```sh
python -m pytest --collect-only -q
```

The Docker runner collects the suite during its build and executes it when started:

```sh
docker run --rm --network service-tests -e SERVICE_BASE_URL=http://service:8080 TEST_IMAGE_ID
```

Both containers must be on the same network. Test generation does not start the
service or execute assertions against it. The verification worker starts both images
after generation succeeds, supplies `SERVICE_WAIT_HOST=service` and
`SERVICE_WAIT_PORT=8080`, and waits up to two minutes for TCP readiness before running
pytest. The trusted runner always limits pytest to ten minutes. Standalone Docker use
can supply the same wait variables, or omit them when the service is already ready.
The JUnit XML report is written to `/tests/reports/junit.xml` and collected by the
verification worker along with bounded container logs.
