#!/usr/bin/env bash
# Trusted host-supplied runner: the arguments are the stubbed modules, named by the host.
# The alias check and stubtest both run, so that the differences of one are not hidden by
# those of the other (such as a `Literal` alias a seam adds first, which the alias check
# reports missing): the check fails when either does.
set -u
allowlist="$(mktemp)"
timeout --signal=TERM --kill-after=10 120 python /literals.py "$allowlist" "$@"
literals=$?
timeout --signal=TERM --kill-after=10 300 \
    python -m mypy.stubtest --ignore-missing-stub --concise \
    --allowlist "$allowlist" --ignore-unused-allowlist "$@"
stubtest=$?
if [ "$stubtest" -ne 0 ]; then
    exit "$stubtest"
fi
exit "$literals"
