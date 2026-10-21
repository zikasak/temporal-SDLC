"""Shared GitHub connection API: one connection for every signed-in user."""

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import http_error
from app.config import GITHUB_ALLOWED_ORIGINS, GITHUB_WEBHOOK_URL
from app.delivery import service as delivery_service
from app.github import service, storage, webhooks
from app.github.client import LOGIN_PATTERN

logger = logging.getLogger(__name__)
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
    # Parse here rather than exposing Pydantic validation input (which can echo a private key).
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 16384:
            raise HTTPException(413, "GitHub configuration input is too large.")
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Use a JSON object for GitHub configuration.") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Use a JSON object for GitHub configuration.")
    return body


def _is_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


_PREFIX_PATTERN = re.compile(r"(/[A-Za-z0-9._~-]+)*")


def _events_url(request: Request) -> str:
    """Where GitHub delivers this instance's webhook events: GITHUB_WEBHOOK_URL when it is
    configured, else this backend's address as the request reached it, under the path
    prefix a reverse proxy strips and reports in X-Forwarded-Prefix (the web client's
    proxies report /api) and with the scheme it reports in X-Forwarded-Proto."""
    if GITHUB_WEBHOOK_URL:
        return GITHUB_WEBHOOK_URL
    prefix = (request.headers.get("x-forwarded-prefix") or "").split(",", 1)[0].strip()
    prefix = prefix.rstrip("/") if _PREFIX_PATTERN.fullmatch(prefix.rstrip("/")) else ""
    scheme = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    url = request.url
    if scheme in {"http", "https"}:
        url = url.replace(scheme=scheme)
    base = str(url.replace(path="", query="", fragment="")).rstrip("/")
    return f"{base}{request.scope.get('root_path', '').rstrip('/')}{prefix}{service.EVENTS_PATH}"


@router.get("/connection")
async def get_connection():
    try:
        return await service.connection_status()
    except Exception as exc:
        raise http_error(exc) from None


@router.put("/connection")
async def connect(body: dict = Depends(mutation_body)):
    app_id, private_key, account, webhook_secret = (
        body.get(key) for key in ("app_id", "private_key", "account", "webhook_secret"))
    if (not {"app_id", "private_key"} <= set(body)
            <= {"app_id", "private_key", "account", "webhook_secret"}
            or type(app_id) is not int or not 0 < app_id < 2 ** 53
            or not isinstance(private_key, str) or not 1 <= len(private_key) <= 8192
            or any(not 32 <= ord(char) <= 126 and char not in "\r\n" for char in private_key)
            or not (account is None or isinstance(account, str) and LOGIN_PATTERN.fullmatch(account))
            # Any string within the request's 16 KiB bound; it only has to be valid
            # Unicode (JSON can carry lone surrogates) to be stored and signed with.
            or not (webhook_secret is None or isinstance(webhook_secret, str)
                    and _is_unicode(webhook_secret))):
        raise HTTPException(422, "Provide the GitHub App's numeric ID, its private key in PEM format "
                                 "and, optionally, the account it is installed on and its "
                                 "webhook secret.")
    try:
        return await service.connect(app_id, private_key, account, webhook_secret or None)
    except Exception as exc:
        raise http_error(exc) from None


@router.post("/connection/manifest")
async def connect_created_app(body: dict = Depends(mutation_body)):
    code = body.get("code")
    if set(body) != {"code"} or not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", code):
        raise HTTPException(422, "Provide the code GitHub returned after creating the app.")
    try:
        return await service.connect_created_app(code)
    except Exception as exc:
        raise http_error(exc) from None


@router.post("/connection/check")
async def check_connection(request: Request, body: dict = Depends(mutation_body)):
    if body:
        raise HTTPException(422, "Send an empty JSON object.")
    try:
        return await service.check_connection(_events_url(request))
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


# GitHub calls this without a user token; its signature authenticates it instead.
events_router = APIRouter(prefix="/connectors/github")
# GitHub caps webhook payloads at 25 MiB; comment events are far smaller.
MAX_EVENT_BYTES = 5 * 1024 * 1024


async def hand_over(session_id: str, mention: dict) -> bool:
    try:
        return await delivery_service.receive_mention(session_id, mention)
    except delivery_service.MentionHandled as exc:
        raise webhooks.AlreadyHandled('This comment was already handled.') from exc


# How often mentions whose handover was interrupted are handed over again.
RECOVERY_INTERVAL_SECONDS = 60


async def recover_mentions_loop() -> None:
    """Hand over again, in the background, the mentions a stopped request claimed but
    never handed to their delivery; GitHub does not redeliver on its own."""
    while True:
        try:
            recovered = await webhooks.recover_pending(hand_over)
            if recovered:
                logger.info("Handed over %d interrupted mentions", recovered)
        except Exception:  # noqa: BLE001 - storage may be unavailable for a while
            logger.warning("Could not recover pending mentions", exc_info=True)
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)


@events_router.post("/events", status_code=202, response_class=Response)
async def receive_event(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_EVENT_BYTES:
            raise HTTPException(413, "The webhook delivery is too large.")
    try:
        connection = await storage.get_connection()
        secret = storage.decrypt_webhook_secret(connection)
    except Exception as exc:
        raise http_error(exc) from None
    if secret is None or not webhooks.signature_matches(secret, bytes(payload),
                                                         x_hub_signature_256):
        raise HTTPException(401, "The webhook delivery is not signed with the connection's "
                                 "webhook secret.")
    delivery_id = (x_github_delivery or "").strip()[:200] or None
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "The webhook payload is not JSON.") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "The webhook payload is not a JSON object.")
    try:
        acted = await webhooks.handle(connection, (x_github_event or "").strip() or None,
                                      delivery_id, body, hand_over)
    except webhooks.InvalidEvent as exc:
        raise HTTPException(422, str(exc)) from None
    except webhooks.AlreadyHandled as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception as exc:
        raise http_error(exc) from None
    return Response(status_code=202 if acted else 204)
