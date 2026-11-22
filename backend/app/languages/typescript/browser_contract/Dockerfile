# check=skip=InvalidDefaultArgInFrom
# Trusted runner of a TypeScript part's page contract conformance check: the part built
# with `vite build` from its lock file, and check-pages.mjs, which opens every page of the
# contract at every viewport in headless Chromium with the API answered from the
# contract's fixtures. The base is the pinned Playwright image (BASE_IMAGE, by digest).
# The check runs without a network; only this build installs anything, without running
# the packages' install scripts.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NO_COLOR=1
RUN useradd --create-home --uid 10001 checker && mkdir /runner /site /contract /checks \
    && chown checker:checker /runner /site
USER checker
COPY --chown=checker:checker repository /repository
ARG PROJECT=.
RUN cd "/repository/$PROJECT" && npm ci --ignore-scripts --no-audit --no-fund \
    && node_modules/.bin/vite build --outDir /site --emptyOutDir --logLevel warn \
    && { test -f /site/index.html || { echo "The part's web client did not build an index.html" >&2; exit 1; }; }
COPY --chown=checker:checker package.json package-lock.json /runner/
WORKDIR /runner
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY contract /contract
COPY checks /checks
COPY check-pages.mjs /runner/
ENTRYPOINT ["timeout", "--signal=TERM", "--kill-after=10", "400", "node", "/runner/check-pages.mjs", "--contract", "/contract", "--site", "/site", "--operations", "/checks/operations.json", "--absent", "/checks/absent.json"]
