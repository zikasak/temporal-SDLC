# Trusted runner of a TypeScript part's component contract conformance check: the part
# installed from its lock file, and the stubs, compared by check.mjs with the part's own
# TypeScript compiler and tsconfig.json. The check runs without a network; only this
# build installs anything, without running the packages' install scripts.
FROM node:24-slim
RUN useradd --create-home --uid 10001 checker && mkdir /runner && chown checker:checker /runner
USER checker
COPY --chown=checker:checker repository /repository
ARG PROJECT=.
RUN cd "/repository/$PROJECT" && npm ci --ignore-scripts --no-audit --no-fund
COPY --chown=checker:checker components /components
COPY check.mjs check.sh /runner/
ENV PROJECT=${PROJECT}
WORKDIR /home/checker
ENTRYPOINT ["bash", "/runner/check.sh"]
