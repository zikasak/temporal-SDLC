#!/usr/bin/env bash
# Check that the published component contract (contracts/components/) still describes the
# service's code, as the platform's conformance check does before it routes or adds a seam:
# every stub module, with the same check.sh (literal aliases + mypy stubtest).
#
# Run from anywhere, with the backend's requirements and mypy installed in the current
# Python (CI installs them; locally e.g. `uv run --no-project --python 3.12
# --with mypy==1.18.2 --with-requirements backend/requirements.txt
# backend/tools/check_component_contract.sh`).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
components="$root/contracts/components"
runner="$root/backend/app/languages/python/component_contract"
[ -d "$components" ] || { echo "No published component contract"; exit 0; }
source_root="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source_root", "."))' \
    "$components/runtime.json")"
modules=()
while IFS= read -r module; do modules+=("$module"); done < <(cd "$components" \
    && find . -name '*.pyi' -type f \
    | sed -e 's|^\./||' -e 's|\.pyi$||' -e 's|/__init__$||' -e 's|/|.|g' | sort)
# check.sh calls /literals.py; run its steps here with the same arguments. Not beside any
# source: mypy prefers modules of its working directory to the stubs.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
export PYTHONPATH="$root/$source_root" MYPYPATH="$components" PYTHONDONTWRITEBYTECODE=1
status=0
python "$runner/literals.py" "$work/allowlist" "${modules[@]}" || status=$?
python -m mypy.stubtest --ignore-missing-stub --concise \
    --allowlist "$work/allowlist" --ignore-unused-allowlist "${modules[@]}" || status=$?
exit "$status"
