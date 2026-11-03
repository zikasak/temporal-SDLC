#!/usr/bin/env bash
# Trusted host-supplied runner: the arguments are the stubbed modules, named by the host.
set -eu
allowlist="$(mktemp)"
timeout --signal=TERM --kill-after=10 120 python /literals.py "$allowlist" "$@"
exec timeout --signal=TERM --kill-after=10 300 \
    python -m mypy.stubtest --ignore-missing-stub --concise \
    --allowlist "$allowlist" --ignore-unused-allowlist "$@"
