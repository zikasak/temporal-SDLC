#!/usr/bin/env bash
# Trusted host-supplied runner: no generated commands or worker secrets. The tests are
# in-process, so there is no service to wait for.
set -eu
# 124/137 indicate timeout/forced termination, not assertion failure. SIGINT, which
# pytest handles as an interruption, so it still writes the report of the tests it ran.
exec timeout --signal=INT --kill-after=10 600 "$@"
