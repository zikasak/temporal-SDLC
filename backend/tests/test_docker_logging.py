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
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'OPENAI_API_KEY': 'test-api-key', 'PRIVATE_TOKEN': 'hidden-provider-token',
           'OUTCOME': outcome}
    caplog.set_level(logging.DEBUG, logger='app.docker_agents')
    destination = tmp_path / 'artifacts/image-build.log'
    operation = docker_agents.docker(
        'build', '--build-arg', 'PASSWORD=never-log-this', '.', env=env,
        # Long enough for the fake to start and print on a loaded machine, well short of
        # its sleep.
        timeout=3 if outcome == 'timeout' else 5, log_path=destination,
    )
    if outcome == 'cancel':
        task = asyncio.create_task(operation)
        for _ in range(500):
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
    state = '{"Running":true,"Pid":146000,"ExitCode":0}'
    env = {'SUMMARY_MAX_TOKENS': '46000', 'AUTH_TOKEN_TTL_HOURS': '12',
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


async def test_stage_turn_budgets_reach_the_agent_container(tmp_path, monkeypatch):
    """A worker's *_MAX_TURNS setting is forwarded by name into `docker create`."""
    calls = []

    async def fake_docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'create':
            raise RuntimeError('stop after create')
        return ''

    async def fake_settings(role):
        return docker_agents.ModelSettings()

    monkeypatch.setattr(docker_agents, 'docker', fake_docker)
    monkeypatch.setattr(docker_agents, 'get_workflow_settings', fake_settings)
    monkeypatch.setattr(docker_agents, 'pi_container_args', lambda settings: [])
    monkeypatch.setattr(docker_agents, 'claude_container_args', lambda settings: [])
    monkeypatch.setattr(docker_agents, 'container_trace_environment', lambda *a: {})
    monkeypatch.setenv('IMPLEMENTATION_MAX_TURNS', '400')
    monkeypatch.delenv('REVIEW_MAX_TURNS', raising=False)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()

    with pytest.raises(RuntimeError, match='stop after create'):
        await docker_agents.run_agent(
            'implementation', 's1', workspace, tmp_path / 'out', 'out', tmp_path / 'agent.log',
            image_build=docker_agents.AgentImageBuild(context=tmp_path, dockerfile=tmp_path / 'Dockerfile'),
            paused=tmp_path / 'paused')

    create = next(args for args in calls if args[0] == 'create')
    pairs = list(zip(create, create[1:]))
    assert ('-e', 'IMPLEMENTATION_MAX_TURNS') in pairs
    assert ('-e', 'REVIEW_MAX_TURNS') not in pairs
    # The value never travels as an argument; container names hold random hex, so match
    # the value as a whole argument or an assignment rather than as any substring.
    assert '400' not in create
    assert not any('MAX_TURNS=' in arg or arg.endswith('=400') for arg in create)


def test_claude_agent_containers_use_the_five_minute_prompt_cache(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test-key')
    args = docker_agents.claude_container_args(
        docker_agents.ModelSettings(adapter='claude', model='claude-sonnet-4-5'))
    assert ('-e', 'FORCE_PROMPT_CACHING_5M=1') in list(zip(args, args[1:]))
