"""The Python conformance runner: the alias check and stubtest both report, and the check
fails when either does."""
import os
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).parents[1] / 'app/languages/python/component_contract/check.sh'
# Stands in for both steps: prints what it was run as and exits as the test asks.
FAKE_PYTHON = '''#!/usr/bin/env bash
if [ "$1" = /literals.py ]; then
    echo "literals $3"
    exit "$LITERALS_EXIT"
fi
echo "stubtest ${@: -1}"
exit "$STUBTEST_EXIT"
'''


@pytest.mark.parametrize('literals, stubtest, status', [
    (0, 0, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1),
])
def test_both_steps_run_and_either_fails_the_check(tmp_path, literals, stubtest, status):
    fake = tmp_path / 'python'
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    result = subprocess.run(
        ['bash', str(CHECK), 'app.delivery.models'], capture_output=True, text=True,
        env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
             'LITERALS_EXIT': str(literals), 'STUBTEST_EXIT': str(stubtest)})
    # stubtest runs even when the alias check failed, so neither hides the other.
    assert result.stdout.splitlines() == ['literals app.delivery.models',
                                          'stubtest app.delivery.models']
    assert result.returncode == status
