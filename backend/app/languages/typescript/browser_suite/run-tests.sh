#!/usr/bin/env bash
# Trusted host-supplied runner: no generated commands or worker secrets. The static server
# of the built client is started by Playwright on 127.0.0.1, and the page's requests to
# the API are answered from the page contract's fixtures, so nothing else is waited for.
set -eu
mkdir -p "$(dirname "$REPORT")"
# 124/137 indicate timeout/forced termination, not assertion failure.
exec timeout --signal=TERM --kill-after=10 600 "$@"
