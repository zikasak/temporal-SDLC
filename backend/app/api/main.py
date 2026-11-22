import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.api.errors import (
    TemporalUnavailableError, request_validation_response, temporal_unavailable_response,
)
from app.api.routes import (
    api_design, architecture, auth, github, llm_requests, onboarding, requirements, settings,
    workflows,
)
from app.auth.storage import ensure_schema as ensure_auth_schema
from app.delivery.acceptances import ensure_schema as ensure_acceptance_schema
from app.delivery.mentions import ensure_schema as ensure_mention_schema
from app.documents import cleanup_expired_documents, ensure_document_schema
from app.logging_setup import configure_logging
from app.github.storage import ensure_schema as ensure_github_schema
from app.onboarding.service import cancel_old_shape_onboardings_loop
from app.onboarding.storage import ensure_schema as ensure_onboarding_schema
from app.llm.pi_credentials import refresh_pi_credentials_loop
from app.temporal.client import TemporalUnavailable
from app.temporal.registry import ensure_schema as ensure_registry_schema
from app.settings.storage import ensure_schema as ensure_settings_schema

configure_logging()
logger = logging.getLogger(__name__)


async def _cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired_documents()
        except (OSError, psycopg.Error):
            logger.exception("Could not clean up expired documents")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await ensure_document_schema()
    await ensure_github_schema()
    await ensure_auth_schema()
    await ensure_registry_schema()
    await ensure_settings_schema()
    await ensure_mention_schema()
    await ensure_acceptance_schema()
    await ensure_onboarding_schema()
    tasks = [asyncio.create_task(_cleanup_loop()), asyncio.create_task(refresh_pi_credentials_loop()),
             asyncio.create_task(github.recover_mentions_loop()),
             # Onboardings from before they ran under task flows are cancelled once.
             asyncio.create_task(cancel_old_shape_onboardings_loop())]
    logger.info("API started")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        logger.info("API stopped")


app = FastAPI(lifespan=lifespan)
# Temporal failures answer 503, or 404 for a session or request this instance never started.
app.add_exception_handler(TemporalUnavailableError, temporal_unavailable_response)
app.add_exception_handler(TemporalUnavailable, temporal_unavailable_response)
# Validation errors answer the Error schema: one readable message, without the inputs.
app.add_exception_handler(RequestValidationError, request_validation_response)
app.include_router(auth.router)
# Called by GitHub, authenticated by its signature instead of a user token.
app.include_router(github.events_router)
# Everything else requires a signed-in user.
for route_module in (llm_requests, requirements, architecture, api_design, onboarding, workflows,
                     github, settings):
    app.include_router(route_module.router, dependencies=[Depends(auth.require_user)])


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s failed", request.method, request.url.path)
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    logger.log(level, "%s %s %d %.0fms", request.method, request.url.path,
               response.status_code, duration_ms)
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
