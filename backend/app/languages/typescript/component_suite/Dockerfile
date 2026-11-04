# Trusted runner of a TypeScript part's component tests: the suite, written from the
# component contract's stubs alone, run in-process in jsdom against the part's sources,
# which only this build adds. The tests run without a network; only this build installs
# anything, without running the packages' install scripts.
FROM node:24-slim
RUN useradd --create-home --uid 10001 tester && mkdir /tests /runner \
    && chown tester:tester /tests /runner
USER tester
COPY --chown=tester:tester repository /repository
ARG PROJECT=.
RUN cd "/repository/$PROJECT" && npm ci --ignore-scripts --no-audit --no-fund
COPY --chown=tester:tester suite /tests
WORKDIR /tests
RUN npm ci --ignore-scripts --no-audit --no-fund
RUN test -x node_modules/.bin/vitest || { echo "The suite does not install vitest" >&2; exit 1; }
COPY vitest.config.mjs run-tests.sh /runner/
ENV PART=/repository/${PROJECT} REPORT=/tests/reports/junit.xml NO_COLOR=1
# Collects the suite: a test that does not even import fails the build.
RUN node_modules/.bin/vitest list --config /runner/vitest.config.mjs > /dev/null
ENTRYPOINT ["bash", "/runner/run-tests.sh"]
CMD ["node_modules/.bin/vitest", "run", "--config", "/runner/vitest.config.mjs"]
