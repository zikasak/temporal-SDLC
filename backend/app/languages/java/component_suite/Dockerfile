# Trusted runner of the component tests: the suite, written from the component contract's
# stubs alone, compiled and run in-process against the service's classes, which only this
# build adds. The tests run without a network; only this build downloads anything.
FROM maven:3.9.9-eclipse-temurin-21
RUN useradd --create-home --uid 10001 tester && mkdir /tests /runner \
    && chown tester:tester /tests /runner
USER tester
# JUnit's console launcher, which runs the suite and writes its reports.
RUN mvn -B -q org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy \
    -Dartifact=org.junit.platform:junit-platform-console-standalone:1.11.4 \
    -DoutputDirectory=/runner -Dmdep.stripVersion=true
COPY --chown=tester:tester build.sh build-suite.sh run-tests.sh /runner/
COPY --chown=tester:tester repository /repository
ARG PROJECT=.
RUN bash /runner/build.sh "/repository/$PROJECT" /runner/service
COPY --chown=tester:tester suite /tests
WORKDIR /tests
RUN bash /runner/build-suite.sh /runner
ENTRYPOINT ["bash", "/runner/run-tests.sh"]
CMD ["java", "-jar", "/runner/junit-platform-console-standalone.jar", "@/runner/console.args"]
