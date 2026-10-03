import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

import psycopg
from fastapi import FastAPI, Request

from app.api.routes import api_design, hello, llm_requests, requirements, workflows
from app.documents import cleanup_expired_documents, ensure_document_schema
from app.logging_setup import configure_logging

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
    cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("API started")
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        logger.info("API stopped")


app = FastAPI(lifespan=lifespan)
app.include_router(hello.router)
app.include_router(llm_requests.router)
app.include_router(requirements.router)
app.include_router(api_design.router)
app.include_router(workflows.router)


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
