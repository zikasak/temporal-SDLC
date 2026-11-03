"""Opt-in check of the Python component contract's conformance runner with real Docker.

Run: RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_component_contract_docker.py -q
Requires Docker and outbound access to fetch the base image and mypy.
"""
import json
import os

import pytest

from app.languages import get_language
from app.routing.components import check_conformance

CONTRACT = get_language('python').component_contract

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DOCKER_VERIFICATION_TESTS') != '1', reason='real Docker tests are opt-in',
)

SERVICE = '''from typing import Literal

import yaml

State = Literal["open", "closed"]


class Flow:
    def close(self, merged: bool) -> None:
        self.state: State = "closed"

    def fork(self, session: str) -> str:
        return yaml.safe_dump(session)
'''
# Only part of the class: the seam, not everything the module has.
STUB = '''from typing import Literal

State = Literal["open", "closed"]


class Flow:
    def close(self, merged: bool) -> None:
        """Closes the flow whether its pull request was merged or not."""
        ...
'''


@pytest.mark.parametrize('stub, error', [
    (STUB, None),
    # A seam the code does not have, and a signature the code does not have.
    (STUB + '\ndef reopen() -> None: ...\n', 'reopen is not present at runtime'),
    (STUB.replace('merged: bool', 'merged: bool, quietly: bool'), 'Flow.close is inconsistent'),
    # stubtest cannot compare a Literal alias; the runner does.
    (STUB.replace('"closed"]', '"closed", "merged"]'), 'State is inconsistent'),
])
async def test_stubs_conform_to_the_code_they_describe_and_nothing_else(tmp_path, stub, error):
    repository = tmp_path / 'repository'
    (repository / 'src/service').mkdir(parents=True)
    (repository / 'src/service/flows.py').write_text(SERVICE)
    # The module imports a dependency only its requirements file installs.
    (repository / 'requirements.txt').write_text('pyyaml\n')
    components = tmp_path / 'components'
    (components / 'service').mkdir(parents=True)
    (components / 'runtime.json').write_text(json.dumps(
        {'source_root': 'src', 'requirements': ['requirements.txt']}))
    (components / 'service/flows.pyi').write_text(stub)
    work = tmp_path / 'work'
    work.mkdir()
    check = check_conformance(CONTRACT, repository, components, ['service.flows'], work,
                              tmp_path / 'logs')
    if error is None:
        await check
    else:
        with pytest.raises(RuntimeError, match=error):
            await check
