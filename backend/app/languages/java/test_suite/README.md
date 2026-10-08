# Service contract tests

This standalone Maven/JUnit project tests the service over HTTP using requirements
and the public API contract. It needs Java 21 and Maven, or its built Docker image.

Start the implemented service and wait until it is ready, then run:

```sh
SERVICE_BASE_URL=http://localhost:8080 mvn -B test
```

`SERVICE_BASE_URL` is required and must point at the running service. Supply credentials through
environment variables when the API requires authentication; document their names
here when adding those tests. Tests must fail when the service is unavailable.

Compile without contacting the service:

```sh
mvn -B -DskipTests -Dmaven.test.skip=false test-compile
```

The Docker runner compiles the suite during its build and executes it when started:

```sh
docker run --rm --network service-tests -e SERVICE_BASE_URL=http://service:8080 TEST_IMAGE_ID
```

Both containers must be on the same network. Test generation does not start the
service or execute assertions against it. The verification worker starts both images
after generation succeeds, supplies `SERVICE_WAIT_HOST=service` and
`SERVICE_WAIT_PORT=8080`, and waits up to two minutes for TCP readiness before running
Maven. The trusted runner always limits Maven to ten minutes. Standalone Docker use
can supply the same wait variables, or omit them when the service is already ready.
Surefire reports are written to `/tests/target/surefire-reports` and collected by the
verification worker along with bounded container logs.
