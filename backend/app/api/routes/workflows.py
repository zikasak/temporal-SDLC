from fastapi import APIRouter, HTTPException

from app.api.errors import http_error
from app.temporal.executions import WORKFLOW_TYPES, InvalidPageToken, list_executions

router = APIRouter(prefix="/workflows")


@router.get("")
async def list_workflows(type: str | None = None, page_token: str | None = None):
    if type and type not in WORKFLOW_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown workflow type: {type}")
    try:
        return await list_executions(type or None, page_token or None)
    except InvalidPageToken as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise http_error(exc) from exc
