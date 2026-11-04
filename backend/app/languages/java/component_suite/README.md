# Component tests

This standalone Maven/JUnit 5 project tests behaviour the HTTP contract tests cannot
observe, at seams of the service's code: the types described by the stubs in
`contracts/components/` of the repository. The tests use the stubbed types and drive them
in-process, with fakes of every collaborator the stubs name. They never fake the seam
itself.

The tests compile against the service's classes, so they are not run with `mvn test`
from this directory alone. To compile them without the service, against the stubs:

```sh
java Stubs.java skeletons ../contracts/components /tmp/skeletons
mvn -B -q org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath \
    -Dmdep.outputFile=/tmp/classpath.txt -DincludeScope=test
javac -proc:none -d /tmp/check -cp "$(cat /tmp/classpath.txt)" \
    $(find /tmp/skeletons src/test/java -name '*.java')
```

`Stubs.java` is the delivery's trusted stub tool. The trusted Docker runner builds the
service's Maven project, puts its classes and dependencies on the class path ahead of the
suite's own (JUnit comes with the runner's console launcher of the suite's JUnit
release), compiles the tests during the build and runs them without a network when
started. A Temporal workflow is tested with `io.temporal:temporal-testing`, whose
in-memory test server needs no network. The run is limited to ten minutes, and the JUnit
XML report is written to `/tests/target/surefire-reports/TEST-junit-jupiter.xml`.
