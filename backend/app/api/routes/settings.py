from fastapi import APIRouter, HTTPException

from app.api.errors import http_error
from app.settings import storage
from app.settings.models import WORKFLOW_LABELS, Adapter, WorkflowSettings
from app.settings.providers import get_provider_options

router = APIRouter(prefix='/settings')


@router.get('')
async def read_settings():
    try:
        settings = await storage.get_settings()
        return {**settings.model_dump(), 'workflow_labels': WORKFLOW_LABELS,
                'providers': await get_provider_options()}
    except Exception as exc:
        raise http_error(exc) from exc


@router.get('/providers')
async def read_providers():
    return await get_provider_options()


@router.put('', response_model=WorkflowSettings)
async def update_settings(body: WorkflowSettings):
    try:
        adapters: list[Adapter] = [item.adapter for item in body.workflows.values()]
        providers = await get_provider_options(adapters)
        for workflow, item in body.workflows.items():
            provider = providers[item.adapter]
            if provider.error:
                raise HTTPException(status_code=503, detail=provider.error)
            model = next((model for model in provider.models if model.id == item.model), None)
            if model is None or item.thinking_level not in model.thinking_levels:
                raise HTTPException(status_code=422, detail=(
                    f'{WORKFLOW_LABELS[workflow]}: the selected model and thinking level '
                    'are no longer available. Refresh provider options and choose again.'))
        return await storage.save_settings(body)
    except Exception as exc:
        raise http_error(exc) from exc
