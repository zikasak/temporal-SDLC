import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from app.api.errors import http_error, validation_detail
from app.api.routes.auth import require_user
from app.auth import storage as auth_storage
from app.settings import endpoint as endpoints
from app.settings import storage
from app.settings.endpoint import EndpointError, OpenAIEndpoint, OpenAIEndpointInput
from app.settings.models import WORKFLOW_LABELS, Adapter, WorkflowSettings
from app.settings.providers import ProviderOptions, get_provider_options

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/settings')


class SettingsUpdate(WorkflowSettings):
    """The body of PUT /settings: every workflow type's settings and, optionally, the
    OpenAI endpoint (an object saves it, null removes it, absent leaves it)."""

    model_config = ConfigDict(extra='forbid')

    openai_endpoint: OpenAIEndpointInput | None = None


class _Conflict(Exception):
    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(body['detail'])
        self.body = body


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, EndpointError):
        return HTTPException(status_code=exc.status_code, detail=exc.detail)
    return http_error(exc)


async def _is_admin(user: dict) -> bool:
    # By the database rather than the token's own claim, like the account administration.
    return user['id'] == await auth_storage.first_user_id()


def _view(settings: WorkflowSettings, providers: dict[str, ProviderOptions],
          stored: endpoints.StoredEndpoint | None, admin: bool) -> dict[str, Any]:
    return {**settings.model_dump(), 'workflow_labels': WORKFLOW_LABELS,
            'providers': {adapter: options.model_dump() for adapter, options in providers.items()},
            'openai_endpoint': endpoints.view(stored, admin=admin)}


def _validation_error(exc: ValidationError) -> HTTPException:
    # Without the inputs: they could hold the API key.
    return HTTPException(status_code=422, detail=validation_detail(
        [{**error, 'loc': ('body', *error['loc'])} for error in exc.errors()]))


async def _stored_providers(stored: endpoints.StoredEndpoint | None,
                            ) -> tuple[OpenAIEndpoint | None, dict[str, ProviderOptions]]:
    """The saved endpoint with its key decrypted, and the provider options it offers.

    A saved key that can no longer be decrypted does not fail the whole response: the
    openai provider then reports it as its error, so the administrator can still load
    the Settings page and enter the key again."""
    try:
        endpoint = endpoints.resolve(stored)
    except EndpointError as exc:
        assert stored is not None
        providers = await get_provider_options(endpoint=None)
        providers['openai'] = ProviderOptions(label=stored.display_name or endpoints.DEFAULT_LABEL,
                                              error=exc.detail)
        return None, providers
    return endpoint, await get_provider_options(endpoint=endpoint)


@router.get('')
async def read_settings(user: dict = Depends(require_user)):
    try:
        admin = await _is_admin(user)
        stored = await endpoints.get_stored_endpoint()
        settings = await storage.get_settings()
        _, providers = await _stored_providers(stored)
        return _view(settings, providers, stored, admin)
    except Exception as exc:
        raise _error(exc) from exc


@router.get('/providers')
async def read_providers():
    try:
        try:
            endpoint = await endpoints.get_openai_endpoint()
        except EndpointError:
            # The saved key cannot be decrypted: reported on the openai entry instead.
            _, providers = await _stored_providers(await endpoints.get_stored_endpoint())
            return providers
    except Exception as exc:
        raise _error(exc) from exc
    return await get_provider_options(endpoint=endpoint)


async def _endpoint_change(section: OpenAIEndpointInput | None,
                           stored: endpoints.StoredEndpoint | None,
                           ) -> tuple[OpenAIEndpoint | None, bytes | None]:
    """The endpoint the request saves, checked when its base URL or key changes, and its
    key encrypted for storage."""
    if section is None:
        return None, None
    if section.api_key is None:
        if stored is None or section.base_url != stored.base_url:
            raise EndpointError(422, 'Enter the API key for the new base URL, or save an '
                                'empty API key to use the endpoint without one.')
        # Only a request that keeps the saved key reads it.
        current = endpoints.resolve(stored)
        assert current is not None
        endpoint = OpenAIEndpoint(base_url=section.base_url, api_key=current.api_key,
                                  display_name=section.display_name)
        # The display name alone changed: saved without a check, the key as it is.
        return endpoint, stored.encrypted_api_key
    endpoint = OpenAIEndpoint(base_url=section.base_url, api_key=section.api_key,
                              display_name=section.display_name)
    # Fails before the check when the key could not be stored.
    encrypted = endpoints.encrypt_api_key(section.api_key) if section.api_key else None
    await endpoints.check_endpoint(endpoint)
    return endpoint, encrypted


@router.put('', responses={409: {'description': 'Removal blocked by workflow types using openai'}})
async def update_settings(body: Any = Body(...), user: dict = Depends(require_user)):
    try:
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail='The body must be a JSON object.')
        changes_endpoint = 'openai_endpoint' in body
        admin = await _is_admin(user)
        if changes_endpoint and not admin:
            raise HTTPException(status_code=403,
                                detail='Only the administrator can change the OpenAI endpoint.')
        try:
            update = SettingsUpdate.model_validate(body)
        except ValidationError as exc:
            raise _validation_error(exc) from None
        settings = WorkflowSettings(workflows=update.workflows)
        stored = await endpoints.get_stored_endpoint()
        saved = (await storage.get_settings()).workflows
        if changes_endpoint and update.openai_endpoint is None and stored is not None:
            using = [workflow for workflow in WORKFLOW_LABELS
                     if saved[workflow].adapter == 'openai'
                     or settings.workflows[workflow].adapter == 'openai']
            if using:
                names = ', '.join(using)
                raise _Conflict({'detail': (
                    'The OpenAI endpoint cannot be removed while these workflow types use '
                    f'the openai adapter: {names}. Choose another adapter for them first.'),
                    'workflow_types': using})
        if changes_endpoint:
            endpoint, encrypted = await _endpoint_change(update.openai_endpoint, stored)
            providers = await get_provider_options(endpoint=endpoint)
        else:
            encrypted = None
            endpoint, providers = await _stored_providers(stored)
        for workflow, item in settings.workflows.items():
            if item == saved.get(workflow):
                # Kept as saved: such as the defaults, the saved choices sent along with the
                # OpenAI endpoint, which a catalog change must not make unsavable, or a saved
                # openai choice while no endpoint is set up, whose runs fail until it is.
                continue
            provider = providers.get(item.adapter)
            if provider is None:
                raise HTTPException(status_code=422, detail=(
                    f'{WORKFLOW_LABELS[workflow]}: the openai adapter is not offered until an '
                    'OpenAI endpoint is saved. Choose another adapter, or save the endpoint.'))
            if provider.error:
                raise HTTPException(status_code=503, detail=provider.error)
            model = next((model for model in provider.models if model.id == item.model), None)
            if model is None or item.thinking_level not in model.thinking_levels:
                raise HTTPException(status_code=422, detail=(
                    f'{WORKFLOW_LABELS[workflow]}: the selected model and thinking level '
                    'are no longer available. Refresh provider options and choose again.'))
        if changes_endpoint:
            await storage.save_settings(settings, endpoint, encrypted)
            stored = await endpoints.get_stored_endpoint()
            if update.openai_endpoint is None:
                logger.info('Removed the OpenAI endpoint')
            else:
                logger.info('Saved the OpenAI endpoint')
        else:
            await storage.save_settings(settings)
        return _view(settings, providers, stored, admin)
    except _Conflict as exc:
        return JSONResponse(status_code=409, content=exc.body)
    except Exception as exc:
        raise _error(exc) from exc
