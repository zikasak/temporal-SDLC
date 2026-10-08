"""Shared GitHub connection API: one connection for every signed-in user."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import http_error
from app.config import GITHUB_ALLOWED_ORIGINS
from app.github import service, storage

router = APIRouter(prefix="/connectors/github")


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=3, max_length=140)
    branch: str = Field(min_length=1, max_length=255)


async def mutation_body(request: Request) -> dict:
    origin = request.headers.get("origin")
    if origin is not None and origin not in GITHUB_ALLOWED_ORIGINS:
        raise HTTPException(403, "This browser origin is not allowed to configure GitHub.")
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(415, "Use application/json for GitHub configuration.")
    # Parse here rather than exposing Pydantic validation input (which can echo a PAT).
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 4096:
            raise HTTPException(413, "GitHub configuration input is too large.")
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Use a JSON object for GitHub configuration.") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Use a JSON object for GitHub configuration.")
    return body


@router.get("/connection")
async def get_connection():
    try:
        return await service.connection_status()
    except Exception as exc:
        raise http_error(exc) from None


@router.put("/connection")
async def connect(body: dict = Depends(mutation_body)):
    token = body.get("token")
    if (set(body) != {"token"} or not isinstance(token, str) or not 1 <= len(token) <= 512
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise HTTPException(422, "Provide a nonempty personal access token without whitespace.")
    try:
        return await service.connect(token)
    except Exception as exc:
        raise http_error(exc) from None


@router.post("/connection/check")
async def check_connection(body: dict = Depends(mutation_body)):
    if body:
        raise HTTPException(422, "Send an empty JSON object.")
    try:
        return await service.check_connection()
    except Exception as exc:
        raise http_error(exc) from None


@router.delete("/connection", status_code=204)
async def disconnect(body: dict = Depends(mutation_body)):
    if body:
        raise HTTPException(422, "Send an empty JSON object.")
    try:
        await storage.disconnect()
    except Exception as exc:
        raise http_error(exc) from None
    return Response(status_code=204)


@router.get("/repositories")
async def repositories(page: int = Query(default=1, ge=1, le=10000)):
    try:
        return await service.list_repositories(page)
    except Exception as exc:
        raise http_error(exc) from None


@router.get("/repositories/{owner}/{repo}/branches")
async def branches(owner: str, repo: str, page: int = Query(default=1, ge=1, le=10000)):
    try:
        return await service.list_branches(f"{owner}/{repo}", page)
    except Exception as exc:
        raise http_error(exc) from None
