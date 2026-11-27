#!/bin/sh
# The repository's checks: builds every part and runs its own tests, as CI does. The
# delivery pipeline's verification runs this script over every change before it is
# pushed (backend/app/verification/checks.py), and so can anyone, from the repository
# root: sh .sdlc/checks.sh
# It needs Python 3.12, Node.js with npm and network access; not Docker or secrets.
# The contract tests in contract-tests/ need a running service and are not run here.
#
# Run again in the same place, it reuses the virtualenv and node_modules while the
# dependency files they were made from are unchanged. The tests run in parallel
# (pytest-xdist; PYTEST_WORKERS sets how many, default 2: the sandbox gives the checks
# two CPUs, and more workers than CPUs make tests with short deadlines flaky; a worker
# that runs out of tests takes those still queued on another), with short
# tracebacks: the failures, not the source of the libraries they passed through, are what
# a reader needs.
set -eu

workers="${PYTEST_WORKERS:-2}"
venv="${TMPDIR:-/tmp}/sdlc-checks-venv"
if ! cmp -s backend/requirements.txt "$venv/.requirements.txt" 2>/dev/null; then
  rm -rf "$venv"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --disable-pip-version-check -r backend/requirements.txt
  cp backend/requirements.txt "$venv/.requirements.txt"
fi
. "$venv/bin/activate"

echo '== Backend tests'
(cd backend && python -m pytest -q -p no:cacheprovider --tb=short -n "$workers")

echo '== Component tests'
(cd component-tests && PYTHONPATH=../backend python -m pytest -q -p no:cacheprovider --tb=short -n "$workers" --dist worksteal)

echo '== Frontend static check'
(
  tool=backend/app/languages/typescript/static_check
  stamp=$tool/node_modules/.sdlc-dependencies
  if ! cat $tool/package.json $tool/package-lock.json | cmp -s - "$stamp" 2>/dev/null; then
    npm ci --prefix $tool --no-audit --no-fund
    cat $tool/package.json $tool/package-lock.json > "$stamp"
  fi
  (cd $tool && node --test)
  $tool/node_modules/.bin/eslint --config $tool/eslint.config.mjs frontend
)

echo '== Frontend typecheck and build'
(
  cd frontend
  stamp=node_modules/.sdlc-dependencies
  if ! cat package.json package-lock.json | cmp -s - "$stamp" 2>/dev/null; then
    npm ci --no-audit --no-fund
    cat package.json package-lock.json > "$stamp"
  fi
  npm run build
)
