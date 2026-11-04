#!/usr/bin/env bash
# Trusted host-supplied runner: the arguments are the stubbed types, named by the host.
set -eu
exec timeout --signal=TERM --kill-after=10 300 \
    java -cp /runner/tool Stubs check /components "$(cat /runner/service/classes.path)" \
    /runner/service/compile.classpath "$@"
