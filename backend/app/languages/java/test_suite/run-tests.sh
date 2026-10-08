#!/usr/bin/env bash
# Trusted host-supplied runner: no generated commands or worker secrets.
set -eu
if [[ -n "${SERVICE_WAIT_HOST:-}" ]]; then
    deadline=$((SECONDS + 120))
    until timeout 2 bash -c 'exec 3<>/dev/tcp/$1/$2' probe "$SERVICE_WAIT_HOST" "${SERVICE_WAIT_PORT:-8080}" 2>/dev/null; do
        if (( SECONDS >= deadline )); then
            echo 'Verification service readiness timed out' >&2
            exit 125
        fi
        sleep 1
    done
fi
# 124/137 indicate timeout/forced termination, not assertion failure.
exec timeout --signal=TERM --kill-after=10 600 "$@"
