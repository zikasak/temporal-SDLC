"""Keep Pi's stored OAuth credentials refreshed ahead of the agents that use them.

OpenAI rotates the refresh token as soon as it answers a refresh, and Pi only saves the
new one afterwards. An agent's Pi process that is killed or times out in between (its
container removed, an activity cancelled) leaves a spent refresh token in auth.json and
every later run fails with `invalid_grant`. Refreshing here, in the long-lived API, keeps
the access tokens valid for longer than Pi's five-minute refresh window, so short-lived
agent processes never need to refresh at all.
"""
import asyncio
import json
import logging
import os

from app.asyncio_utils import run_sync
from app.config import PI_CREDENTIALS_DIR

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 600
# Longer than the interval, so a token never drops into an agent's refresh window.
MIN_VALIDITY = '30m'
# Pi bounds its own refresh at 15 seconds. A slower process is left to finish, never
# killed: killing it mid-refresh is what loses tokens.
COMMAND_TIMEOUT_SECONDS = 120
MAX_ERROR_CHARS = 2000


def _oauth_providers() -> list[str]:
    try:
        stored = json.loads((PI_CREDENTIALS_DIR / 'auth.json').read_text())
    # Missing, or read while Pi rewrites it in place: the next round reads it whole.
    except (FileNotFoundError, ValueError):
        return []
    return [provider for provider, credential in stored.items()
            if isinstance(credential, dict) and credential.get('type') == 'oauth']


async def refresh_pi_credentials() -> None:
    """Have Pi refresh, under its own lock, each OAuth token expiring within MIN_VALIDITY."""
    for provider in await run_sync(_oauth_providers):
        try:
            process = await asyncio.create_subprocess_exec(
                'pi', 'auth', 'print-bearer-token', '--provider', provider,
                '--min-expiry', MIN_VALIDITY,
                env={**os.environ, 'PI_CODING_AGENT_DIR': str(PI_CREDENTIALS_DIR)},
                # The token itself is printed; it must not reach the logs.
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            logger.warning('Pi is not installed; its OAuth credentials cannot be refreshed')
            return
        _, stderr = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT_SECONDS)
        if process.returncode:
            logger.error('Pi could not refresh the %s OAuth credentials. If this persists, '
                         'sign in again with pi as the API user. %s', provider,
                         stderr.decode('utf-8', 'replace').strip()[-MAX_ERROR_CHARS:])


async def refresh_pi_credentials_loop() -> None:
    while True:
        try:
            await refresh_pi_credentials()
        except (OSError, TimeoutError):
            logger.exception('Could not refresh Pi OAuth credentials')
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
