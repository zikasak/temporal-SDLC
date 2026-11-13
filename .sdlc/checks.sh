#!/bin/sh
# The repository's checks: builds every part and runs its own tests, as CI does. The
# delivery pipeline's verification runs this script over every change before it is
# pushed (backend/app/verification/checks.py), and so can anyone, from the repository
# root: sh .sdlc/checks.sh
# It needs Python 3.12, Node.js with npm and network access; not Docker or secrets.
# The contract tests in contract-tests/ need a running service and are not run here.
set -eu

venv="${TMPDIR:-/tmp}/sdlc-checks-venv"
python3 -m venv "$venv"
. "$venv/bin/activate"
pip install --quiet --disable-pip-version-check -r backend/requirements.txt

echo '== Backend tests'
(cd backend && python -m pytest -q)

echo '== Component tests'
(cd component-tests && PYTHONPATH=../backend python -m pytest -q)

echo '== Frontend typecheck and build'
(cd frontend && npm ci --no-audit --no-fund && npm run build)
