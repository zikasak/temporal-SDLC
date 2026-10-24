from fastapi import APIRouter, HTTPException

from app.api.errors import http_error
from app.api.routes.onboarding import SESSION_ID_PATTERN as ONBOARDING_ID_PATTERN
from app.github.client import NAME_PATTERN
from app.temporal.executions import WORKFLOW_TYPES, InvalidPageToken, list_executions

router = APIRouter(prefix="/workflows")


def _validate_filters(
    type: str | None, repository: str | None, branch: str | None, onboarding_id: str | None,
) -> None:
    if type and type not in WORKFLOW_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown workflow type: {type}")
    if repository is not None and (
        not NAME_PATTERN.fullmatch(repository) or repository.split("/")[-1] in {".", ".."}
    ):
        raise HTTPException(status_code=422,
                            detail="The repository filter must be of the form owner/name.")
    if branch is not None and not branch:
        raise HTTPException(status_code=422, detail="The branch filter must not be empty.")
    if onboarding_id is not None and not ONBOARDING_ID_PATTERN.fullmatch(onboarding_id):
        raise HTTPException(
            status_code=422,
            detail="The onboarding_id filter must be onboarding-session- followed by 32 hex "
                   "digits.")


@router.get("")
async def list_workflows(
    type: str | None = None,
    page_token: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    onboarding_id: str | None = None,
):
    _validate_filters(type, repository, branch, onboarding_id)
    try:
        return await list_executions(type or None, page_token or None, repository, branch,
                                     onboarding_id)
    except InvalidPageToken as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise http_error(exc) from exc
