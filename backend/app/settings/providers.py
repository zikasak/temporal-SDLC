"""Discover selectable models from the same providers used for model runs.

Discovery never sends a prompt. Missing capability metadata means only the
provider's default is safe; model names are never used to guess capabilities.
"""
import asyncio
import json
import os
import tempfile
from collections.abc import Iterable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.asyncio_utils import async_context
from app.config import OPENAI_API_KEY, OPENAI_BASE_URL, PI_CREDENTIALS_DIR
from app.settings.models import Adapter


class ModelOption(BaseModel):
    id: str
    label: str
    thinking_levels: list[str] = Field(default_factory=lambda: ['default'])


class ProviderOptions(BaseModel):
    label: str
    models: list[ModelOption] = Field(default_factory=list)
    error: str | None = None
    hint: str | None = None


class ProviderDiscoveryError(RuntimeError):
    """An actionable, credential-free discovery error safe to show in settings."""


async def _openai_models() -> list[ModelOption]:
    headers = {'Authorization': f'Bearer {OPENAI_API_KEY}'} if OPENAI_API_KEY else {}
    async with httpx.AsyncClient(timeout=10) as client:
        if urlparse(OPENAI_BASE_URL).hostname == 'generativelanguage.googleapis.com':
            # The factory uses the native Google SDK for this endpoint as well.
            models = []
            params = {}
            while True:
                response = await client.get(
                    'https://generativelanguage.googleapis.com/v1beta/models',
                    headers={'x-goog-api-key': OPENAI_API_KEY}, params=params)
                response.raise_for_status()
                body = response.json()
                models.extend(ModelOption(id=item['name'].removeprefix('models/'),
                                          label=item.get('displayName') or item['name'])
                              for item in body['models']
                              if 'generateContent' in item.get('supportedGenerationMethods', []))
                if not body.get('nextPageToken'):
                    return models
                params = {'pageToken': body['nextPageToken']}
        response = await client.get(f'{OPENAI_BASE_URL.rstrip("/")}/models', headers=headers)
        response.raise_for_status()
        return [ModelOption(id=item['id'], label=item['id']) for item in response.json()['data']]


async def _claude_models() -> list[ModelOption]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    # Match bare-model execution: no project settings, tools or persisted session.
    async with async_context(tempfile.TemporaryDirectory, prefix='claude-models-') as cwd:
        options = ClaudeAgentOptions(cwd=cwd, tools=[], setting_sources=[],
                                     extra_args={'no-session-persistence': None})
        async with ClaudeSDKClient(options=options) as client:
            info = await client.get_server_info()
    models = []
    for item in (info or {}).get('models', []):
        levels = ['default']
        # ChatClaude sends effort together with adaptive thinking.
        if item.get('supportsEffort') and item.get('supportsAdaptiveThinking'):
            levels.extend(item.get('supportedEffortLevels', []))
        models.append(ModelOption(id=item['value'], label=item['displayName'],
                                  thinking_levels=list(dict.fromkeys(levels))))
    return models


async def _pi_models() -> list[ModelOption]:
    try:
        process = await asyncio.create_subprocess_exec(
            'pi', '--mode', 'rpc', '--no-session', '--no-tools', '--no-extensions',
            '--no-skills', '--no-prompt-templates', '--no-context-files', '--no-themes',
            '--no-approve',
            env={**os.environ, 'PI_CODING_AGENT_DIR': str(PI_CREDENTIALS_DIR)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
    except FileNotFoundError as exc:
        raise ProviderDiscoveryError(
            'Pi is not installed or is not on the API server PATH. Install Pi on the '
            'server running the API, then restart the API and refresh provider options.') from exc
    except PermissionError as exc:
        raise ProviderDiscoveryError(
            'The API server cannot execute Pi. Check the Pi executable permissions '
            'for the API user, then refresh provider options.') from exc

    stdin, stdout = process.stdin, process.stdout
    assert stdin is not None and stdout is not None  # both piped

    async def command(kind: str, **params):
        stdin.write((json.dumps({'id': kind, 'type': kind, **params}) + '\n').encode())
        await stdin.drain()
        while line := await stdout.readline():
            event = json.loads(line)
            if event.get('type') == 'response' and event.get('id') == kind:
                if not event.get('success'):
                    raise ProviderDiscoveryError(
                        'Pi rejected a model discovery command. Check its credentials '
                        'and update Pi on the API server, then refresh provider options.')
                return event['data'] if 'data' in event else {}
        code = await process.wait()
        raise ProviderDiscoveryError(
            f'Pi exited during model discovery (exit code {code}). Check that Pi starts '
            'for the API user and that PI_CREDENTIALS_DIR points to its configuration.')

    try:
        models = []
        for item in (await command('get_available_models'))['models']:
            # Ask Pi itself to resolve model-specific restrictions, including
            # custom models and provider-specific mappings, without generating.
            await command('set_model', provider=item['provider'], modelId=item['id'])
            levels = (await command('get_available_thinking_levels'))['levels']
            models.append(ModelOption(id=f'{item["provider"]}/{item["id"]}',
                                      label=f'{item["name"]} ({item["provider"]})',
                                      thinking_levels=list(dict.fromkeys(['default', *levels]))))
        if not models:
            raise ProviderDiscoveryError(
                'Pi has no selectable models. Sign in to Pi as the API user and check '
                'PI_CREDENTIALS_DIR, then refresh provider options.')
        return models
    except (json.JSONDecodeError, KeyError) as exc:
        raise ProviderDiscoveryError(
            'Pi returned an incompatible model discovery response. Update Pi on the '
            'API server, then refresh provider options.') from exc
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()


PROVIDERS = {
    'pi': ('Pi', _pi_models),
    'openai': ('OpenAI', _openai_models),
    'claude': ('Claude', _claude_models),
}


async def get_provider_options(adapters: Iterable[Adapter] | None = None) -> dict[str, ProviderOptions]:
    async def discover(adapter):
        label, discover_models = PROVIDERS[adapter]
        try:
            models = await asyncio.wait_for(discover_models(), timeout=20)
            models = sorted({item.id: item for item in models}.values(), key=lambda item: item.label.lower())
            return adapter, ProviderOptions(
                label=label, models=models,
                error=None if models else f'{label} reported no available models.',
                hint=('This endpoint does not report supported thinking levels; only Model default is available.'
                      if adapter == 'openai' else None))
        except ProviderDiscoveryError as exc:
            return adapter, ProviderOptions(label=label, error=str(exc))
        except TimeoutError:
            return adapter, ProviderOptions(
                label=label, error=f'{label} model discovery timed out. Check that the '
                'provider starts for the API user, then refresh provider options.')
        except Exception:
            # Provider errors can contain credentials or private endpoint URLs.
            return adapter, ProviderOptions(label=label, error=f'Could not load {label} models. Check the provider connection and retry.')

    return dict(await asyncio.gather(*(discover(adapter) for adapter in
                                      (PROVIDERS if adapters is None else sorted(set(adapters))))))
