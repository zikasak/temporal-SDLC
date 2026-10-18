import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from starlette.datastructures import UploadFile

from app.asyncio_utils import run_sync
from app.api.errors import http_error
from app.api.routes.requirements import ForkBody
from app.api.routes.github import RepositoryInput
from app.github.service import resolve_repository_selection
from app.conversations.errors import ConversationNotReady, ConversationTooLarge
from app.conversations.service import (
    fork_llm_request,
    get_llm_request,
    retry_llm_request,
    start_llm_request,
)
from app.documents import (
    MAX_UPLOAD_BYTES,
    DocumentNotFound,
    delete_document,
    get_document,
    get_generated_document,
    original_path,
    store_original,
)
from app.temporal.sessions import InvalidForkPoint

router = APIRouter()
REQUEST_ID_PATTERN = re.compile(r"llm-request-[0-9a-f]{32}")
DOCUMENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class LLMRequestBody(BaseModel):
    message: str = Field(max_length=10000)
    parent_request_id: str | None = Field(
        default=None, pattern=r"^llm-request-[0-9a-f]{32}$"
    )
    # Reply to this answer in the parent's conversation, as a new branch.
    fork_after: int | None = Field(default=None, ge=0)
    repository: RepositoryInput | None = None

    @field_validator("message")
    @classmethod
    def require_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        return value

    @model_validator(mode="after")
    def require_parent_for_fork(self) -> "LLMRequestBody":
        if self.fork_after is not None and not self.parent_request_id:
            raise ValueError("fork_after needs a parent_request_id")
        if self.parent_request_id and "repository" in self.model_fields_set:
            raise ValueError("Repository selection is inherited and cannot be changed")
        return self


def _check_request_id(request_id: str) -> None:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HTTPException(status_code=404, detail="Request not found")


async def _request_body(request: Request) -> tuple[LLMRequestBody, UploadFile | None]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                raise HTTPException(status_code=413, detail="The request is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length.") from exc
    try:
        if media_type == "application/json":
            return LLMRequestBody.model_validate(await request.json()), None
        if media_type == "multipart/form-data":
            form = await request.form(max_files=1, max_fields=4)
            file = form.get("file")
            if file is not None and not isinstance(file, UploadFile):
                raise HTTPException(status_code=422, detail="File must be an upload.")
            if len(form.getlist("file")) > 1:
                raise HTTPException(status_code=422, detail="Attach one file per message.")
            body = LLMRequestBody.model_validate(
                {"message": form.get("message"),
                 "parent_request_id": form.get("parent_request_id") or None,
                 "fork_after": form.get("fork_after") or None,
                 **({"repository": json.loads(str(form["repository"]))} if "repository" in form else {})}
            )
            return body, file
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=415, detail="Use JSON or multipart form data.")


@router.post(
    "/llm/requests",
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {
                            "message": {"type": "string", "maxLength": 10000},
                            "parent_request_id": {"type": "string"},
                            "fork_after": {"type": "integer", "minimum": 0},
                            "repository": {"type": "object", "properties": {
                                "full_name": {"type": "string"}, "branch": {"type": "string"},
                            }, "required": ["full_name", "branch"]},
                        },
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["message"],
                        "properties": {
                            "message": {"type": "string", "maxLength": 10000},
                            "parent_request_id": {"type": "string"},
                            "fork_after": {"type": "integer", "minimum": 0},
                            "file": {"type": "string", "format": "binary"},
                            "repository": {"type": "string", "description": "JSON object with full_name and branch; standalone requests only"},
                        },
                    }
                },
            },
        }
    },
)
async def submit_llm_request(request: Request):
    body, upload = await _request_body(request)
    attachment = None
    try:
        repository = await resolve_repository_selection(
            body.repository.full_name, body.repository.branch,
        ) if body.repository else None
        if upload:
            attachment = await store_original(upload)
        request_id = await start_llm_request(
            body.message, body.parent_request_id, attachment, body.fork_after, repository
        )
    except Exception as exc:
        if attachment and isinstance(
            exc, (ConversationNotReady, ConversationTooLarge, DocumentNotFound,
                  InvalidForkPoint)
        ):
            await delete_document(attachment["document_id"])
        raise http_error(exc) from exc
    finally:
        if upload:
            await upload.close()
    return {"request_id": request_id, "status": "running"}


@router.post("/llm/requests/{request_id}/retry", status_code=202)
async def retry_request(request_id: str):
    _check_request_id(request_id)
    try:
        new_request_id = await retry_llm_request(request_id)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"request_id": new_request_id, "status": "running"}


@router.post("/llm/requests/{request_id}/fork", status_code=202)
async def fork_request(request_id: str, body: ForkBody):
    """Branch after one of your messages and answer it again."""
    _check_request_id(request_id)
    try:
        new_request_id = await fork_llm_request(request_id, body.message_index)
    except Exception as exc:
        raise http_error(exc) from exc
    return {"request_id": new_request_id, "status": "running"}


@router.get("/llm/requests/{request_id}")
async def retrieve_llm_request(request_id: str):
    _check_request_id(request_id)
    try:
        record = await get_llm_request(request_id)
        return {key: value for key, value in record.items() if key != "session_id"}
    except Exception as exc:
        raise http_error(exc) from exc


@router.get("/llm/requests/{request_id}/documents/{document_id}")
async def download_document(request_id: str, document_id: str):
    _check_request_id(request_id)
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        record = await get_llm_request(request_id)
        is_attachment = any(
            item.get("attachment", {}).get("document_id") == document_id
            for item in record["messages"]
        )
        if is_attachment:
            document = await get_document(document_id)
        else:
            document = await get_generated_document(document_id, record["session_id"])
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from exc
    if not is_attachment:
        return Response(
            content=document["parsed_text"].encode("utf-8"),
            media_type=document["content_type"],
            headers={"Content-Disposition":
                     f"attachment; filename*=utf-8''{quote(document['filename'])}"},
        )
    path = original_path(document)
    if not await run_sync(path.is_file):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(
        path, media_type=document["content_type"], filename=document["filename"]
    )
