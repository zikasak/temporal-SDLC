"""Refresh stored Pi OAuth credentials through a real subprocess standing in for `pi`."""
import asyncio
import json
import logging
import os
import sys

from app.llm import pi_credentials

# Prints a token, as Pi does, and fails for the provider whose refresh token was spent.
FAKE_PI = '''#!{python}
import json, os, pathlib, sys
log = pathlib.Path({log!r})
calls = json.loads(log.read_text()) if log.exists() else []
calls.append({{'argv': sys.argv[1:], 'agent_dir': os.environ.get('PI_CODING_AGENT_DIR')}})
log.write_text(json.dumps(calls))
print('secret-access-token')
if 'spent' in sys.argv:
    sys.stderr.write('OAuth refresh failed for spent: invalid_grant')
    sys.exit(1)
'''


def test_refreshes_each_oauth_credential_without_logging_tokens(tmp_path, monkeypatch, caplog):
    agent_dir = tmp_path / 'agent'
    agent_dir.mkdir()
    (agent_dir / 'auth.json').write_text(json.dumps({
        'openai': {'type': 'oauth', 'access': 'a', 'refresh': 'r', 'expires': 0},
        'anthropic': {'type': 'api_key', 'key': 'k'},
        'spent': {'type': 'oauth', 'access': 'a', 'refresh': 'r', 'expires': 0},
    }))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'calls.json'
    script = bin_dir / 'pi'
    script.write_text(FAKE_PI.format(python=sys.executable, log=str(log)))
    script.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    monkeypatch.setattr(pi_credentials, 'PI_CREDENTIALS_DIR', agent_dir)

    with caplog.at_level(logging.INFO, logger=pi_credentials.__name__):
        asyncio.run(pi_credentials.refresh_pi_credentials())

    assert json.loads(log.read_text()) == [
        {'argv': ['auth', 'print-bearer-token', '--provider', provider, '--min-expiry', '30m'],
         'agent_dir': str(agent_dir)}
        for provider in ('openai', 'spent')
    ]
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert 'spent' in caplog.text and 'invalid_grant' in caplog.text
    assert 'secret-access-token' not in caplog.text
