"""Opt-in proof of Coolify Pi credentials across two Docker daemons.

Build the application image first, then run:
  RUN_DOCKER_PI_TESTS=1 PI_TEST_APP_IMAGE=temporal-pi-api:oauth python -m pytest tests/test_pi_credentials_docker.py -q

Requires Docker and python:3.12-slim (pulled when absent). The disposable nested
daemon uses privileged runc for this test; production retains Sysbox. No real
OAuth credentials, provider requests, or host Docker socket mounts are used.
"""
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DOCKER_PI_TESTS') != '1', reason='real Pi Docker test is opt-in',
)
ROOT = Path(__file__).resolve().parents[2]
SOCKET = 'unix:///run/sandbox/docker.sock'


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, capture_output=True,
                          text=True, timeout=90, **kwargs).stdout


def test_pi_credentials_rotate_across_daemons_and_survive_container_replacement(tmp_path):
    # Coolify strips its extension keys before giving the document to Compose.
    manifest = yaml.safe_load((ROOT / 'docker-compose.coolify.yml').read_text())
    for service in manifest['services'].values():
        service.pop('exclude_from_hc', None)
    compose = tmp_path / 'compose.yml'
    compose.write_text(yaml.safe_dump(manifest))
    config = json.loads(docker(
        'compose', '--env-file', '/dev/null', '--project-directory', str(ROOT),
        '-f', str(compose), 'config', '--format', 'json',
        env={**os.environ, 'SERVICE_PASSWORD_POSTGRES': 'test-password',
             'SERVICE_PASSWORD_64_JWT': 'test-signing-secret-for-compose-validation'}))
    services = config['services']
    credential_path = services['api']['environment']['PI_CREDENTIALS_DIR']
    shared_volume = next(mount['source'] for mount in services['api']['volumes']
                         if mount['target'] == credential_path)
    # Validate the effective deployment, including the worker volume overrides.
    for name, service in services.items():
        if 'PI_CREDENTIALS_DIR' not in service.get('environment', {}):
            continue
        assert service['environment']['PI_CODING_AGENT_DIR'] == credential_path
        assert service['environment']['PI_CREDENTIALS_SHARED'] == 'true'
        assert any(mount['source'] == shared_volume and mount['target'] == credential_path
                   for mount in service['volumes']), name
        assert service['depends_on']['pi-credentials-init']['condition'] == 'service_completed_successfully'
    assert any(mount['source'] == shared_volume and mount['target'] == credential_path
               for mount in services['sandbox']['volumes'])

    image = os.getenv('PI_TEST_APP_IMAGE', 'temporal-pi-api:oauth')
    docker('image', 'inspect', image)
    child_image = 'python:3.12-slim'
    if subprocess.run(['docker', 'image', 'inspect', child_image], capture_output=True).returncode:
        docker('pull', child_image)
    token = uuid.uuid4().hex[:12]
    credentials = f'pi-oauth-test-{token}-credentials'
    socket = f'pi-oauth-test-{token}-socket'
    daemon = f'pi-oauth-test-{token}-daemon'
    docker('volume', 'create', credentials)
    docker('volume', 'create', socket)

    def application(script, *, worker=False):
        options = ['-v', f'{credentials}:{credential_path}']
        if worker:
            options += ['-v', f'{socket}:/run/sandbox', '-e', f'DOCKER_HOST={SOCKET}']
        return docker('run', '--rm', '--user', '10001:10001', *options,
                      '-e', f'PI_CREDENTIALS_DIR={credential_path}',
                      '-e', f'PI_CODING_AGENT_DIR={credential_path}',
                      '-e', 'PI_CREDENTIALS_SHARED=true', '--entrypoint', 'python', image, '-c', script)

    try:
        init = services['pi-credentials-init']
        docker('run', '--rm', '--user', init['user'], '-v', f'{credentials}:{credential_path}',
               '--entrypoint', init['entrypoint'][0], image,
               *init['entrypoint'][1:], *init['command'])
        application('''
import json, os
from pathlib import Path
root = Path(os.environ['PI_CODING_AGENT_DIR'])
assert root.stat().st_uid == 10001 and root.stat().st_mode & 0o777 == 0o700
(root / 'auth.json').write_text(json.dumps({'generation': 1}))
''')
        docker('run', '-d', '--privileged', '--name', daemon,
               '-v', f'{credentials}:{credential_path}', '-v', f'{socket}:/run/sandbox',
               '--entrypoint', 'dockerd', 'docker:29-dind',
               f'--host={SOCKET}', '--group=10001', '--iptables=false', '--bridge=none',
               '--storage-driver=vfs')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', daemon, 'docker', '-H', SOCKET, 'info'],
                                   capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            pytest.fail('Nested Docker daemon did not become ready')
        with tempfile.TemporaryFile() as archive:
            subprocess.run(['docker', 'image', 'save', child_image], stdout=archive,
                           stderr=subprocess.PIPE, check=True, timeout=90)
            archive.seek(0)
            subprocess.run(['docker', 'exec', '-i', daemon, 'docker', '-H', SOCKET, 'load'],
                           stdin=archive, capture_output=True, check=True, timeout=90)

        # Exercise the production mount builder from a worker container connected
        # only to the nested daemon, whose bind paths differ from the outer host.
        application('''
import json, os, subprocess
from pathlib import Path
from app.docker_agents import pi_container_args
from app.settings.models import ModelSettings
root = Path(os.environ['PI_CREDENTIALS_DIR'])
assert json.loads((root / 'auth.json').read_text()) == {'generation': 1}
args = pi_container_args(ModelSettings(adapter='pi', model='openai-codex/test'))
def child(script):
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '10001:10001',
                    *args, 'python:3.12-slim', 'python', '-c', script], check=True, timeout=30)
lock = root / 'auth.json.lock'
lock.mkdir()
try:
    child("from pathlib import Path; assert Path('/home/agent/.pi/agent/auth.json.lock').is_dir()")
finally:
    lock.rmdir()
child("""
import json, os
from pathlib import Path
root = Path('/home/agent/.pi/agent')
lock = root / 'auth.json.lock'
lock.mkdir()
try:
    assert json.loads((root / 'auth.json').read_text()) == {'generation': 1}
    updated = root / 'updated-auth.json'
    updated.write_text(json.dumps({'generation': 2}))
    os.replace(updated, root / 'auth.json')
finally:
    lock.rmdir()
""")
assert json.loads((root / 'auth.json').read_text()) == {'generation': 2}
assert not lock.exists()
''', worker=True)
        # A new API container sees the agent's replacement after both previous
        # containers have exited, proving persistence rather than a copied snapshot.
        application('''
import json, os
from pathlib import Path
assert json.loads((Path(os.environ['PI_CODING_AGENT_DIR']) / 'auth.json').read_text()) == {'generation': 2}
''')
    finally:
        subprocess.run(['docker', 'rm', '-f', daemon], capture_output=True, timeout=30)
        subprocess.run(['docker', 'volume', 'rm', credentials, socket], capture_output=True, timeout=30)
