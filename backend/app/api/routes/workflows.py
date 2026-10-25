from fastapi import APIRouter, HTTPException

from app.api.errors import http_error
from app.temporal.executions import (
    WORKFLOW_TYPES, InvalidFilter, InvalidPageToken, check_filters, list_executions,
)

router = APIRouter(prefix="/workflows")


@router.get("")
async def list_workflows(type: str | None = None, page_token: str | None = None,
                         repository: str | None = None, branch: str | None = None,
                         onboarding_id: str | None = None):
    if type and type not in WORKFLOW_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown workflow type: {type}")
    try:
        # Malformed filters answer 422 before Temporal is asked.
        check_filters(repository, branch, onboarding_id)
    except InvalidFilter as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await list_executions(type or None, page_token or None, repository=repository,
                                     branch=branch, onboarding_id=onboarding_id)
    except (InvalidPageToken, InvalidFilter) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise http_error(exc) from exc
