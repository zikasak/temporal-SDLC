#!/usr/bin/env bash
# Trusted host-supplied runner: no generated commands or worker secrets. The tests are
# in-process, so there is no service to wait for.
set -eu
# 124/137 indicate timeout/forced termination, not assertion failure.
exec timeout --signal=TERM --kill-after=10 600 "$@"
