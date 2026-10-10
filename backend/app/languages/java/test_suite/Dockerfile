FROM maven:3.9.9-eclipse-temurin-21
RUN useradd --create-home --uid 10001 tester && mkdir /tests && chown tester:tester /tests
WORKDIR /tests
COPY --chown=tester:tester . .
USER tester
RUN mvn -B -DskipTests -Dmaven.test.skip=false test-compile
ENTRYPOINT ["bash", "/tests/run-tests.sh"]
CMD ["mvn", "-B", "-DfailIfNoTests=true", "-DskipTests=false", "-Dmaven.test.skip=false", "-Dsurefire.reportsDirectory=/tests/target/surefire-reports", "test"]
