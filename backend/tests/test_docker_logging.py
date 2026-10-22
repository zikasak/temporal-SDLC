"""Docker subprocess diagnostics at the real process boundary (no daemon required)."""
import asyncio
import logging
import os
import sys

import pytest

from app import docker_agents


@pytest.mark.parametrize('outcome', ['success', 'failure', 'timeout', 'cancel'])
async def test_docker_preserves_redacted_bounded_diagnostics_and_reaps_process(
    tmp_path, monkeypatch, caplog, outcome,
):
    executable = tmp_path / 'docker'
    ready = tmp_path / 'ready'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import os, pathlib, sys, time\n'
        'print("old output" * 2000, flush=True)\n'
        'print(os.environ["OPENAI_API_KEY"], flush=True)\n'
        'print(os.environ["PRIVATE_TOKEN"], file=sys.stderr, flush=True)\n'
        'print("compiler or Docker diagnostics", file=sys.stderr, flush=True)\n'
        f'pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n'
        'if os.environ["OUTCOME"] in {"timeout", "cancel"}: time.sleep(10)\n'
        'sys.exit(1 if os.environ["OUTCOME"] == "failure" else 0)\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', 'test-api-key')
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'OPENAI_API_KEY': 'test-api-key', 'PRIVATE_TOKEN': 'hidden-provider-token',
           'OUTCOME': outcome}
    caplog.set_level(logging.DEBUG, logger='app.docker_agents')
    destination = tmp_path / 'artifacts/image-build.log'
    operation = docker_agents.docker(
        'build', '--build-arg', 'PASSWORD=never-log-this', '.', env=env,
        timeout=0.5 if outcome == 'timeout' else 5, log_path=destination,
    )
    if outcome == 'cancel':
        task = asyncio.create_task(operation)
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists(), 'fake Docker did not start'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome != 'success':
        expected = TimeoutError if outcome == 'timeout' else RuntimeError
        with pytest.raises(expected) as exc:
            await operation
        assert 'test-api-key' not in str(exc.value) and 'hidden-provider-token' not in str(exc.value)
    else:
        output = await operation
        assert '[redacted]' in output and 'compiler or Docker diagnostics' in output
        assert 'test-api-key' not in output and 'hidden-provider-token' not in output
        assert len(output) <= 8000
    persisted = destination.read_text()
    assert len(persisted) < 8200 and 'compiler or Docker diagnostics' in persisted
    assert '[redacted]' in persisted
    for text in (persisted, caplog.text):
        assert not any(secret in text for secret in ('test-api-key', 'hidden-provider-token', 'never-log-this'))
    assert 'started' in caplog.text and 'finished' in caplog.text
    if outcome == 'timeout':
        assert 'TimeoutError' in persisted
    if outcome == 'cancel':
        assert 'CancelledError' in persisted
    with pytest.raises(ProcessLookupError):
        os.kill(int(ready.read_text()), 0)


def test_short_settings_named_like_secrets_do_not_corrupt_output(monkeypatch):
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', '')
    state = '{"Running":true,"Pid":146000,"ExitCode":0}'
    env = {'AGENT_CONTEXT_TOKENS': '46000', 'AUTH_TOKEN_TTL_HOURS': '12',
           'PRIVATE_TOKEN': 'hidden-provider-token'}
    assert docker_agents.redact_output(state, env) == state
    assert docker_agents.redact_output('hidden-provider-token', env) == '[redacted]'


async def test_container_output_reaches_the_worker_log_at_its_own_level(tmp_path, monkeypatch, caplog):
    """A sandboxed container's output, as `docker logs --follow` streams it, is re-logged
    line by line at the level each line names, with known secrets redacted."""
    executable = tmp_path / 'docker'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import sys\n'
        'assert sys.argv[1:] == ["logs", "--follow", "agent-1"], sys.argv\n'
        'print("Implementation completed")\n'
        'print("2025-01-01T00:00:00Z  ERROR 1 --- [main] o.s.Boot: Port in use")\n'
        'print("\\tat o.s.Boot.run(Boot.java:1)")\n'
        'print("12:00:00.000 [main] WARN  c.e.Orders - Slow query")\n'
        'print("DEBUG app.llm.pi: request sent", file=sys.stderr)\n'
        'print("ERROR app.agent: failed with hidden-provider-token", file=sys.stderr)\n'
        'print("  File \\"/opt/agent.py\\", line 1", file=sys.stderr)\n'
        'print("npm notice something", file=sys.stderr, end="")\n'
    )
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr(docker_agents, 'OPENAI_API_KEY', '')
    caplog.set_level(logging.DEBUG, logger='app.containers')

    async with docker_agents.forward_logs('agent-1', 'implementation agent s1',
                                          {'PRIVATE_TOKEN': 'hidden-provider-token'}):
        await asyncio.sleep(0)

    records = {(record.levelname, record.getMessage()) for record in caplog.records
               if record.name == 'app.containers'}
    label = '[implementation agent s1] '
    assert records == {(level, label + line) for level, line in [
        ('INFO', 'Implementation completed'),
        ('ERROR', '2025-01-01T00:00:00Z  ERROR 1 --- [main] o.s.Boot: Port in use'),
        ('ERROR', '\tat o.s.Boot.run(Boot.java:1)'),
        ('WARNING', '12:00:00.000 [main] WARN  c.e.Orders - Slow query'),
        ('DEBUG', 'DEBUG app.llm.pi: request sent'),
        ('ERROR', 'ERROR app.agent: failed with [redacted]'),
        ('ERROR', '  File "/opt/agent.py", line 1'),
        ('WARNING', 'npm notice something'),
    ]}


async def test_unavailable_container_logs_never_fail_the_observed_run(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))  # no docker executable at all
    async with docker_agents.forward_logs('agent-1', 'agent'):
        pass
