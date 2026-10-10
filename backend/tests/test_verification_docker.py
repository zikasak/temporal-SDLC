"""Opt-in end-to-end verification of real images, networking and test reports.

Run: RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_verification_docker.py -q
Requires Docker and outbound access to fetch base images/Maven dependencies.
"""
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from app import docker_agents
from app.languages import get_language
from app.verification import activities

TEMPLATE = get_language('java').test_suite.template

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_DOCKER_VERIFICATION_TESTS') != '1', reason='real Docker tests are opt-in',
)


@pytest.fixture(scope='session')
async def images(tmp_path_factory):
    root = tmp_path_factory.mktemp('verification-docker')
    service = root / 'service'
    service.mkdir()
    (service / 'server.py').write_text('''import os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
mode = os.environ['MODE']
if mode == 'unavailable':
    time.sleep(3600)
if mode == 'delayed':
    time.sleep(3)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'contract response')
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
''')
    (service / 'Dockerfile').write_text('''FROM python:3.12-slim
ARG MODE=ready
ENV MODE=$MODE
WORKDIR /app
COPY server.py .
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]
''')
    built = {}

    async def build(context, key, *options):
        iid = root / f'{key}.iid'
        await docker_agents.docker('build', '--iidfile', str(iid), *options, str(context))
        built[key] = iid.read_text().strip()

    try:
        for mode in ('ready', 'delayed', 'unavailable'):
            await build(service, mode, '--build-arg', f'MODE={mode}')
        for expected in (200, 201):
            runner = root / f'runner-{expected}'
            shutil.copytree(TEMPLATE, runner)
            source = runner / 'src/test/java/example/HttpContractTest.java'
            source.parent.mkdir(parents=True)
            source.write_text('''package example;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
public class HttpContractTest {
    @Test public void verifiesHttpContract() throws Exception {
        var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        var request = HttpRequest.newBuilder(URI.create(System.getenv("SERVICE_BASE_URL") + "/"))
            .timeout(Duration.ofSeconds(5)).GET().build();
        var response = client.send(request, HttpResponse.BodyHandlers.ofString());
        assertEquals(EXPECTED, response.statusCode());
        assertEquals("contract response", response.body());
    }
}
'''.replace('EXPECTED', str(expected)))
            await build(runner, f'runner-{expected}')
        yield built
    finally:
        for image in set(built.values()):
            await docker_agents.docker('image', 'rm', image, timeout=30)


@pytest.mark.parametrize('mode,expected,status', [
    ('ready', 200, 'passed'), ('ready', 201, 'failed'),
    ('delayed', 200, 'passed'), ('unavailable', 200, 'error'),
])
async def test_real_service_and_test_runner(images, tmp_path, monkeypatch, mode, expected, status):
    monkeypatch.setattr(activities, 'GENERATED_CODE_DIR', tmp_path)
    session_id = f'docker-verification-{uuid.uuid4().hex}'
    result = await ActivityEnvironment().run(
        activities.verify_service, session_id, images[mode], images[f'runner-{expected}'], 'java',
    )
    assert result['status'] == status, result
    assert json.loads((tmp_path / session_id / 'verification/result.json').read_text()) == result
    if status != 'error':
        assert result['tests'] == 1
        assert result['passed'] == int(status == 'passed')
        assert result['failures'] == int(status == 'failed')
        assert list((Path(result['path']) / 'reports').glob('TEST-*.xml'))
    else:
        assert result['exit_code'] == 125 and result['error']
    assert not await docker_agents.docker('ps', '-aq', '--filter', f'label=temporal.verification={session_id}')
    assert not await docker_agents.docker('network', 'ls', '-q', '--filter', f'label=temporal.verification={session_id}')
