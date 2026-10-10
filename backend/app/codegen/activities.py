from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.codegen.generator import CodegenError, generate_server
from app.languages import UnsupportedLanguage, get_language
from app.config import GENERATED_CODE_DIR
from app.documents import DocumentNotFound, get_generated_document
from app.temporal.tracing import trace_activity


@activity.defn
@trace_activity('generate-server-code', feature='codegen', input_parameter='document_id')
async def generate_server_code(
    session_id: str, document_id: str, language: str,
) -> dict[str, Any]:
    try:
        generator = get_language(language)
    except UnsupportedLanguage as exc:
        activity.logger.warning("Code generation refused for %s: %s", session_id, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    try:
        document = await get_generated_document(document_id, session_id)
    except DocumentNotFound as exc:
        activity.logger.warning("OpenAPI document %s is not available: %s", document_id, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    target = GENERATED_CODE_DIR / session_id / "server"
    try:
        files = await generate_server(generator, document["parsed_text"], target)
    except FileNotFoundError as exc:
        activity.logger.warning("openapi-generator is not installed: %s", exc)
        raise ApplicationError(
            f"openapi-generator could not be started: {exc}", non_retryable=True
        ) from exc
    except CodegenError as exc:
        activity.logger.warning("Code generation failed for %s: %s", session_id, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    return {"path": str(target), "files": files, "language": generator.name}
