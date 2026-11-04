#!/usr/bin/env bash
# Trusted host-supplied runner: the arguments are the stubbed modules, named by the host.
set -eu
exec timeout --signal=TERM --kill-after=10 300 \
    node /runner/check.mjs "/repository/$PROJECT" /components "$@"
