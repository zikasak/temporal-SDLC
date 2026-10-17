from fastapi import APIRouter

from app.api.errors import http_error
from app.settings import storage
from app.settings.models import THINKING_LEVELS, WORKFLOW_LABELS, WorkflowSettings

router = APIRouter(prefix='/settings')


@router.get('')
async def read_settings():
    try:
        settings = await storage.get_settings()
        return {**settings.model_dump(), 'workflow_labels': WORKFLOW_LABELS,
                'thinking_levels': THINKING_LEVELS}
    except Exception as exc:
        raise http_error(exc) from exc


@router.put('', response_model=WorkflowSettings)
async def update_settings(body: WorkflowSettings):
    try:
        return await storage.save_settings(body)
    except Exception as exc:
        raise http_error(exc) from exc
