# check=skip=InvalidDefaultArgInFrom
# Trusted runner of a TypeScript part's browser tests: the suite, written from the page
# contract alone, run by Playwright in headless Chromium against the part's built web
# client, which only this build adds. The base is the pinned Playwright image
# (BASE_IMAGE, by digest), whose Chromium and fonts are the ones the suite's
# @playwright/test expects; the host pulls it ahead and builds without pulling. The tests
# run without a network; only this build installs anything, without running the packages'
# install scripts.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NO_COLOR=1
RUN useradd --create-home --uid 10001 tester && mkdir /tests /runner /site /contract \
    && chown tester:tester /tests /runner /site
USER tester
COPY --chown=tester:tester repository /repository
ARG PROJECT=.
RUN cd "/repository/$PROJECT" && npm ci --ignore-scripts --no-audit --no-fund \
    && node_modules/.bin/vite build --outDir /site --emptyOutDir --logLevel warn \
    && { test -f /site/index.html || { echo "The part's web client did not build an index.html" >&2; exit 1; }; }
COPY --chown=tester:tester suite /tests
WORKDIR /tests
RUN npm ci --ignore-scripts --no-audit --no-fund
RUN test -x node_modules/.bin/playwright || { echo "The suite does not install @playwright/test" >&2; exit 1; }
# The image's Chromium is the one this release of @playwright/test expects, and no other
# is downloaded: the suite may not install another release.
ARG PLAYWRIGHT_VERSION
RUN installed="$(node -p "require('@playwright/test/package.json').version")" \
    && { test "$installed" = "$PLAYWRIGHT_VERSION" || { echo "The suite installs @playwright/test $installed, but the runner's browser is that of $PLAYWRIGHT_VERSION: keep @playwright/test at $PLAYWRIGHT_VERSION exactly" >&2; exit 1; }; }
COPY contract /contract
COPY playwright.config.mjs reporter.mjs serve.mjs run-tests.sh /runner/
# What starts every class name of the report: the suite member's name and a colon, so a
# delivery's merged reports tell these tests from its other parts' (`frontend~browser:`).
ARG CLASSNAME_PREFIX=
ENV CONTRACT=/contract SITE=/site REPORT=/tests/reports/junit.xml RESULTS=/tmp/playwright-results \
    CLASSNAME_PREFIX=${CLASSNAME_PREFIX}
# Collects the suite: a test that does not even load fails the build.
RUN node_modules/.bin/playwright test --list --config /runner/playwright.config.mjs > /dev/null
ENTRYPOINT ["bash", "/runner/run-tests.sh"]
CMD ["node_modules/.bin/playwright", "test", "--config", "/runner/playwright.config.mjs"]
