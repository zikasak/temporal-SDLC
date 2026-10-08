import asyncio
import re
import time
from functools import partial
from typing import Any

import psycopg
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.host import session_language
from app.api_design.models import ApiDesignState, ApiModel
from app.api_design.openapi import DOCUMENT_FILENAME as OPENAPI_FILENAME
from app.api_design.openapi import api_problems, render_openapi
from app.api_design.prompt import api_turn_messages
from app.config import GENERATED_CODE_DIR
from app.documents import (
    DocumentConflict,
    DocumentNotFound,
    DocumentParseError,
    DocumentValidationError,
    create_generated_document,
    edit_generated_document,
    list_generated_documents,
    list_session_passages,
    parse_stored_document,
    read_document_text,
)
from app.github import checkout as repository_checkout
from app.github import service as github_service
from app.github import storage as github_storage
from app.github.errors import GitHubError
from app.github.models import RepositoryCheckout, RepositoryPublication, RepositorySelection
from app.implementation.activities import is_service_file
from app.llm.graph import get_message_graph
from app.llm.model import get_model
from app.llm.prompt import INLINE_DOCUMENT_CHARS, MAX_REMEMBERED_CHARS, model_messages
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config
from app.requirements.document import DOCUMENT_FILENAME, render_requirements_markdown
from app.requirements.prompt import document_messages, interview_messages
from app.tools import ToolContext, build_tools
from app.tools import api_design as api_design_tools
from app.tools import repository as repository_tools
from app.tools import requirements as requirements_tools
from app.tools.document_write import summarize_tool_results


def _attached_document_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Attachment ids, newest first."""
    ids: list[str] = []
    for item in reversed(messages):
        attachment = item.get("attachment")
        document_id = attachment.get("document_id") if isinstance(attachment, dict) else None
        if (isinstance(document_id, str) and re.fullmatch(r"[0-9a-f]{32}", document_id)
                and document_id not in ids):
            ids.append(document_id)
    return ids


async def _inline_texts(document_ids: list[str]) -> dict[str, str]:
    """Full text of small attachments, newest first, within one shared budget."""
    texts: dict[str, str] = {}
    budget = 2 * INLINE_DOCUMENT_CHARS
    for document_id in document_ids:
        try:
            text, total = await read_document_text(document_id, 0, INLINE_DOCUMENT_CHARS)
        except DocumentNotFound:
            continue
        if total <= INLINE_DOCUMENT_CHARS and total <= budget:
            texts[document_id] = text
            budget -= total
    return texts


def _repository_tools(session_id: str | None = None) -> list[BaseTool]:
    """Read-only tools over the session's checkout; none for a session without one.

    A session is its workflow, except in Chat, where each message is its own."""
    if not activity.in_activity():
        return []
    root = repository_checkout.checkout_path(
        GENERATED_CODE_DIR, session_id or activity.info().workflow_id)
    return repository_tools.build(root) if root.is_dir() else []


async def _github_token() -> tuple[str, dict[str, Any]]:
    """A fresh installation token and the saved connection it belongs to."""
    try:
        connection = await github_storage.get_connection()
        github_storage.decrypt_private_key(connection)
    except GitHubError as exc:
        # A missing connection or key needs an operator, not another attempt.
        raise ApplicationError(str(exc), non_retryable=True) from exc
    try:
        return await github_service.installation_token(connection), connection
    except GitHubError as exc:
        # Neither do rejected credentials or a removed installation; an unreachable or
        # rate-limiting GitHub may recover.
        raise ApplicationError(
            str(exc), non_retryable=exc.status_code in (401, 403, 409, 422)) from exc


@activity.defn
@trace_activity('checkout-repository', feature='repository', input_parameter='repository')
async def checkout_repository(
    session_id: str, repository: RepositorySelection
) -> RepositoryCheckout:
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    token, _ = await _github_token()
    try:
        result = await repository_checkout.checkout(target.parent, repository, token)
    except (repository_checkout.CheckoutError, GitHubError) as exc:
        activity.logger.warning("Could not check out %s: %s", repository["full_name"], exc)
        raise ApplicationError(str(exc)) from exc
    activity.logger.info("Checked out %s@%s at %s for session %s", repository["full_name"],
                         repository["branch"], result["commit"], session_id)
    return result


@activity.defn
@trace_activity('publish-repository', feature='repository', input_parameter='repository')
async def publish_repository(
    session_id: str, repository: RepositorySelection, language: str | None = None,
) -> RepositoryPublication:
    """Commit the verified service and its contract tests to a new branch and push it."""
    service_files = partial(is_service_file, session_language(language))
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    service = target.parent / 'implementation/service'
    suite = target.parent / 'tests/suite'
    if not all(item.is_dir() for item in (target, service, suite)):
        raise ApplicationError("The verified sources or the repository checkout are unavailable.",
                               non_retryable=True)
    token, connection = await _github_token()
    bot = f"{connection['app_slug']}[bot]"
    try:
        await asyncio.to_thread(repository_checkout.replace_files, service, target, service_files)
        await asyncio.to_thread(repository_checkout.replace_files, suite,
                                target / repository_checkout.TESTS_DIR, lambda relative: True)
        result = await repository_checkout.push(
            target, repository, token,
            branch=repository_checkout.publication_branch(session_id),
            message=("Add generated service and contract tests\n\n"
                     f"Generated from {repository['branch']} by session {session_id}."),
            author=(bot, f"{connection['bot_user_id']}+{bot}@users.noreply.github.com"),
        )
    except (repository_checkout.CheckoutError, GitHubError) as exc:
        activity.logger.warning("Could not push %s: %s", repository["full_name"], exc)
        raise ApplicationError(str(exc)) from exc
    activity.logger.info("Pushed session %s to %s (%s)", session_id, result["branch"],
                         result["commit"])
    return result


@activity.defn
async def greet(name: str) -> str:
    return f"Hello {name}"


@activity.defn
@trace_activity('parse-document', feature='documents', input_parameter='document_id')
async def parse_document(document_id: str) -> None:
    try:
        await parse_stored_document(document_id)
    except (DocumentParseError, DocumentNotFound) as exc:
        activity.logger.warning("Could not parse document %s: %s", document_id, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    activity.logger.info("Parsed document %s", document_id)


@activity.defn
@trace_activity('respond-to-message', feature='chat', as_type='agent')
async def send_llm_message(
    messages: Any, session_id: str | None = None,
    repository: RepositorySelection | None = None,
) -> str:

    # Older workflows can still have a single string as their activity input.
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    session_id = session_id or activity.info().workflow_id
    generated_documents = await list_generated_documents(session_id)
    attached_ids = _attached_document_ids(messages)
    inline_texts = await _inline_texts(attached_ids)
    try:
        remembered = await list_session_passages(
            session_id, set(attached_ids) - inline_texts.keys(), MAX_REMEMBERED_CHARS
        )
    except psycopg.Error:
        activity.logger.warning("Could not load remembered passages", exc_info=True)
        remembered = []
    repository_files = _repository_tools(session_id) if repository else []
    prompt, document_ids = model_messages(
        messages, generated_documents, inline_texts, remembered,
        repository_tools.chat_guidance(repository, bool(repository_files))
        if repository else "",
    )
    activity.logger.info(
        "LLM request for session %s: %d messages, %d attachments (%d inline), "
        "%d remembered passages, %d generated documents, repository %s",
        session_id, len(messages), len(attached_ids), len(inline_texts),
        len(remembered), len(generated_documents),
        "none" if not repository else "checked out" if repository_files else "unavailable",
    )
    started = time.perf_counter()
    try:
        graph = get_message_graph(
            [*build_tools(ToolContext(session_id, document_ids)), *repository_files])
        result = await graph.ainvoke(
            {"messages": prompt}, config=langchain_config(recursion_limit=20, run_name='respond-with-tools')
        )
        answer = str(result["messages"][-1].text).strip()
        if not answer:
            answer = summarize_tool_results(result["messages"])
        if not answer:
            raise ValueError("The model returned no text")
        activity.logger.info("LLM answered in %.1fs with %d characters",
                             time.perf_counter() - started, len(answer))
        return answer
    except ValueError as exc:
        activity.logger.warning("LLM request failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn
@trace_activity('gather-requirements', feature='requirements', as_type='agent')
async def requirements_turn(
    messages: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> dict[str, Any]:
    ctx = requirements_tools.RequirementsContext.from_list(requirements)
    repository = _repository_tools()
    started = time.perf_counter()
    try:
        graph = get_message_graph([*requirements_tools.build(ctx), *repository])
        result = await graph.ainvoke(
            {"messages": interview_messages(
                messages, requirements, repository_tools.GUIDANCE if repository else "")},
            config=langchain_config(recursion_limit=20, run_name='interview-with-tools'),
        )
        reply = str(result["messages"][-1].text).strip()
    except ValueError as exc:
        activity.logger.warning("Requirements turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = ctx.as_list()
    activity.logger.info("Requirements turn took %.1fs: %d -> %d requirements",
                         time.perf_counter() - started, len(requirements), len(updated))
    if not reply:
        if updated == requirements:
            activity.logger.warning("Requirements turn returned no text and no changes")
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = f"Noted. {len(updated)} requirements are recorded so far. What else should I know?"
    return {"reply": reply, "requirements": updated}


async def _save_session_document(
    session_id: str, filename: str, content: str
) -> dict[str, Any]:
    """Create the session's document, or replace it when it already exists."""
    try:
        existing = next(
            (item for item in await list_generated_documents(session_id)
             if item["filename"] == filename),
            None,
        )
        if existing:
            document = await edit_generated_document(
                session_id, existing["document_id"], existing["revision"], "replace",
                content=content,
            )
        else:
            document = await create_generated_document(session_id, filename, content)
    except DocumentValidationError as exc:
        activity.logger.warning("Could not save %s: %s", filename, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except (DocumentNotFound, DocumentConflict) as exc:
        activity.logger.warning("Could not save %s, will retry: %s", filename, exc)
        raise ApplicationError(str(exc)) from exc
    activity.logger.info("Saved %s for session %s (document %s, revision %d)",
                         filename, session_id, document["document_id"], document["revision"])
    return document


@activity.defn
@trace_activity('write-requirements-document', feature='requirements',
                input_parameter='requirements')
async def write_requirements_document(
    session_id: str, messages: list[dict[str, Any]], requirements: list[dict[str, Any]]
) -> dict[str, Any]:
    try:
        answer = await get_model().ainvoke(
            document_messages(messages, requirements),
            config=langchain_config(run_name='write-requirements-overview'),
        )
        narrative = str(answer.text).strip() or None
    except Exception:
        # The requirement tables alone are still a useful document.
        activity.logger.exception("Could not write the requirements overview")
        narrative = None
    content = render_requirements_markdown(requirements, narrative)
    return await _save_session_document(session_id, DOCUMENT_FILENAME, content)


@activity.defn
@trace_activity('design-api', feature='api-design', as_type='agent')
async def api_design_turn(state: ApiDesignState) -> dict[str, Any]:
    ctx = api_design_tools.ApiDesignContext.from_model(
        state.api, {item["id"] for item in state.requirements}, state.language
    )
    repository = _repository_tools()
    started = time.perf_counter()
    try:
        graph = get_message_graph([*api_design_tools.build(ctx), *repository])
        result = await graph.ainvoke(
            {"messages": api_turn_messages(
                state, repository_tools.GUIDANCE if repository else "")},
            config=langchain_config(recursion_limit=40, run_name='draft-api-with-tools')
        )
        reply = str(result["messages"][-1].text).strip()
    except GraphRecursionError:
        # Drafting can take many tool calls; keep what was recorded.
        activity.logger.warning("The API design turn hit the recursion limit")
        reply = ""
    except ValueError as exc:
        activity.logger.warning("API design turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = ctx.as_model()
    activity.logger.info("API design turn took %.1fs: %d endpoints, %d schemas",
                         time.perf_counter() - started, len(updated["endpoints"]),
                         len(updated["schemas"]))
    if not reply:
        if updated == state.api and ctx.language == state.language:
            activity.logger.warning("API design turn returned no text and no changes")
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = (f"I've updated the API: it has {len(updated['endpoints'])} endpoints and "
                 f"{len(updated['schemas'])} schemas so far. Review them below and tell me "
                 "what to change or what else to cover.")
    return {"reply": reply, "api": updated, "language": ctx.language}


@activity.defn
@trace_activity('render-openapi', feature='api-design', input_parameter='api')
async def write_openapi_document(session_id: str, api: ApiModel) -> dict[str, Any]:
    problems = api_problems(api)
    if problems:
        activity.logger.warning("Cannot render OpenAPI: %s", " ".join(problems))
        raise ApplicationError(" ".join(problems), non_retryable=True)
    return await _save_session_document(session_id, OPENAPI_FILENAME, render_openapi(api))
