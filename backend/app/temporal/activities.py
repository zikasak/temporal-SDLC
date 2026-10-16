import asyncio
import re
import time
from collections.abc import Callable
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
from app.delivery.contract import ContractError, RepositoryContract, read_contract, write_contract
from app.documentation.activities import is_documentation_file
from app.documents import (
    DocumentConflict,
    DocumentNotFound,
    DocumentParseError,
    DocumentValidationError,
    create_generated_document,
    edit_generated_document,
    get_generated_document,
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
from app.llm.graph import get_message_graph, interview
from app.llm.model import get_model
from app.llm.prompt import INLINE_DOCUMENT_CHARS, MAX_REMEMBERED_CHARS, model_messages
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config
from app.requirements.document import DOCUMENT_FILENAME, render_requirements_markdown
from app.requirements.models import Requirement
from app.requirements.prompt import document_messages, interview_messages
from app.tools import ToolContext, build_tools
from app.tools import api_design as api_design_tools
from app.tools import questions as question_tools
from app.tools import repository as repository_tools
from app.tools import requirements as requirements_tools
from app.tools.document_write import summarize_tool_results

COMMIT_SUBJECT = "Add generated service, contract tests and contract"
# What the commit of a task that changes an existing service says instead.
COMMIT_SUBJECTS = {"api_change": "Change the service's API, contract tests and contract",
                   "bugfix": "Fix the service and add regression tests"}
DOCUMENTATION_SUBJECT = "Change the documentation"
# Added to the commit of sources that were pushed without a check of them.
UNVERIFIED_NOTE = "Not verified: no check exists for this kind of change yet."


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


def _published_requirements() -> tuple[Requirement, ...]:
    """The requirements in the contract of the session's repository; none for a
    repository this pipeline has not published a service to."""
    if not activity.in_activity():
        return ()
    try:
        contract = read_contract(repository_checkout.checkout_path(
            GENERATED_CODE_DIR, activity.info().workflow_id))
    except ContractError:
        return ()
    return tuple(contract["requirements"])


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
    return await _checkout(session_id, repository, latest=False)


@activity.defn
@trace_activity('update-repository-checkout', feature='repository', input_parameter='repository')
async def update_repository_checkout(
    session_id: str, repository: RepositorySelection
) -> RepositoryCheckout:
    """Check the session's branch out again at its latest commit, such as one that added
    the contract the session's task needs."""
    return await _checkout(session_id, repository, latest=True)


async def _checkout(session_id: str, repository: RepositorySelection,
                    latest: bool) -> RepositoryCheckout:
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    token, _ = await _github_token()
    try:
        result = await repository_checkout.checkout(target.parent, repository, token,
                                                    latest=latest)
    except (repository_checkout.CheckoutError, GitHubError) as exc:
        activity.logger.warning("Could not check out %s: %s", repository["full_name"], exc)
        raise ApplicationError(str(exc)) from exc
    activity.logger.info("Checked out %s@%s at %s for session %s", repository["full_name"],
                         repository["branch"], result["commit"], session_id)
    return result


async def _publish(session_id: str, repository: RepositorySelection, subject: str,
                   update: Callable[[], None], verified: bool = True) -> RepositoryPublication:
    """Apply `update` to the session's checkout, commit it to a new branch and push it."""
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    token, connection = await _github_token()
    bot = f"{connection['app_slug']}[bot]"
    try:
        await asyncio.to_thread(update)
        result = await repository_checkout.push(
            target, repository, token,
            branch=repository_checkout.publication_branch(session_id),
            message=(f"{subject}\n\n"
                     f"Generated from {repository['branch']} by session {session_id}."
                     + ("" if verified else f"\n\n{UNVERIFIED_NOTE}")),
            author=(bot, f"{connection['bot_user_id']}+{bot}@users.noreply.github.com"),
            verified=verified,
        )
    except (repository_checkout.CheckoutError, GitHubError) as exc:
        activity.logger.warning("Could not push %s: %s", repository["full_name"], exc)
        raise ApplicationError(str(exc)) from exc
    activity.logger.info("Pushed session %s to %s (%s)", session_id, result["branch"],
                         result["commit"])
    return result


@activity.defn
@trace_activity('publish-repository', feature='repository', input_parameter='repository')
async def publish_repository(
    session_id: str, repository: RepositorySelection, contract: RepositoryContract,
    kind: str | None = None,
) -> RepositoryPublication:
    """Commit the verified service, its contract tests and the contract they were built
    from to a new branch and push it."""
    service_files = partial(is_service_file, session_language(contract['language']))
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    service = target.parent / 'implementation/service'
    suite = target.parent / 'tests/suite'
    if not all(item.is_dir() for item in (target, service, suite)):
        raise ApplicationError("The verified sources or the repository checkout are unavailable.",
                               non_retryable=True)

    def update() -> None:
        repository_checkout.replace_files(service, target, service_files)
        repository_checkout.replace_files(suite, target / repository_checkout.TESTS_DIR,
                                          lambda relative: True)
        write_contract(target, contract)

    return await _publish(session_id, repository, COMMIT_SUBJECTS.get(kind, COMMIT_SUBJECT),
                          update)


@activity.defn
@trace_activity('publish-documentation', feature='repository', input_parameter='repository')
async def publish_documentation(
    session_id: str, repository: RepositorySelection, language: str, verified: bool,
) -> RepositoryPublication:
    """Commit the documentation its author changed to a new branch and push it. Nothing
    else of the repository changes: not the service, its tests or its contract. Without
    `verified` the commit and the publication say that nothing checked the change."""
    documentation_files = partial(is_documentation_file, session_language(language))
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    written = target.parent / 'documentation/files'
    if not all(item.is_dir() for item in (target, written)):
        raise ApplicationError("The documentation or the repository checkout is unavailable.",
                               non_retryable=True)
    return await _publish(
        session_id, repository,
        DOCUMENTATION_SUBJECT if verified else f"{DOCUMENTATION_SUBJECT} (unverified)",
        lambda: repository_checkout.replace_files(written, target, documentation_files),
        verified)


@activity.defn
@trace_activity('read-repository-contract', feature='repository', input_parameter='session_id')
async def read_repository_contract(session_id: str) -> RepositoryContract:
    """The contract the session's repository was published with, which a task that
    changes its service starts from."""
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    try:
        return await asyncio.to_thread(read_contract, target)
    except ContractError as exc:
        activity.logger.warning("Session %s has no usable contract: %s", session_id, exc)
        # Shown to the user as it is; another attempt reads the same checkout.
        raise ApplicationError(str(exc), type='ContractError', non_retryable=True) from exc


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
    messages: list[dict[str, Any]], requirements: list[dict[str, Any]],
    kind: str | None = None, retired: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    retired = retired or []
    ctx = requirements_tools.RequirementsContext.from_list(
        requirements, kind, await asyncio.to_thread(_published_requirements), retired)
    repository = _repository_tools()
    asked = question_tools.QuestionsContext()
    started = time.perf_counter()
    try:
        graph = get_message_graph(
            [*requirements_tools.build(ctx), *question_tools.build(asked), *repository],
            until=lambda: bool(asked.questions))
        reply = await interview(
            graph, interview_messages(
                messages, requirements, kind,
                repository_tools.GUIDANCE if repository else "",
                list(ctx.service_requirements()), ctx.retired_list()),
            langchain_config(recursion_limit=20, run_name='interview-with-tools'),
            lambda: bool(asked.questions))
    except ValueError as exc:
        activity.logger.warning("Requirements turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = ctx.as_list()
    activity.logger.info("Requirements turn took %.1fs: %d -> %d requirements",
                         time.perf_counter() - started, len(requirements), len(updated))
    if not reply and not asked.questions:
        if updated == requirements and ctx.kind == kind and ctx.retired_list() == retired:
            activity.logger.warning("Requirements turn returned no text and no changes")
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = (f"Noted. {len(updated)} requirements are recorded so far. Tell me what "
                 "else I should know.")
    return {"reply": reply, "questions": asked.questions, "requirements": updated,
            "kind": ctx.kind, "retired": ctx.retired_list()}


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
    session_id: str, messages: list[dict[str, Any]], requirements: list[dict[str, Any]],
    retired: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        answer = await get_model().ainvoke(
            document_messages(messages, requirements, retired),
            config=langchain_config(run_name='write-requirements-overview'),
        )
        narrative = str(answer.text).strip() or None
    except Exception:
        # The requirement tables alone are still a useful document.
        activity.logger.exception("Could not write the requirements overview")
        narrative = None
    content = render_requirements_markdown(requirements, narrative, retired)
    return await _save_session_document(session_id, DOCUMENT_FILENAME, content)


@activity.defn
async def read_requirements_document(session_id: str, document_id: str) -> str:
    """The text of the session's requirements document, as the user downloads it."""
    try:
        return (await get_generated_document(document_id, session_id))["parsed_text"]
    except DocumentNotFound as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn
@trace_activity('design-api', feature='api-design', as_type='agent')
async def api_design_turn(state: ApiDesignState) -> dict[str, Any]:
    ctx = api_design_tools.ApiDesignContext.from_model(
        state.api, {item["id"] for item in state.requirements}, state.language,
        language_fixed=bool(state.change_document),
    )
    repository = _repository_tools()
    asked = question_tools.QuestionsContext()
    started = time.perf_counter()
    try:
        graph = get_message_graph(
            [*api_design_tools.build(ctx), *question_tools.build(asked), *repository],
            until=lambda: bool(asked.questions))
        reply = await interview(
            graph, api_turn_messages(state, repository_tools.GUIDANCE if repository else ""),
            langchain_config(recursion_limit=40, run_name='draft-api-with-tools'),
            lambda: bool(asked.questions))
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
    if not reply and not asked.questions:
        if updated == state.api and ctx.language == state.language:
            activity.logger.warning("API design turn returned no text and no changes")
            raise ApplicationError("The model returned no text", non_retryable=True)
        reply = (f"I've updated the API: it has {len(updated['endpoints'])} endpoints and "
                 f"{len(updated['schemas'])} schemas so far. Review them below and tell me "
                 "what to change or what else to cover.")
    return {"reply": reply, "questions": asked.questions, "api": updated,
            "language": ctx.language}


@activity.defn
@trace_activity('render-openapi', feature='api-design', input_parameter='api')
async def write_openapi_document(session_id: str, api: ApiModel) -> dict[str, Any]:
    problems = api_problems(api)
    if problems:
        activity.logger.warning("Cannot render OpenAPI: %s", " ".join(problems))
        raise ApplicationError(" ".join(problems), non_retryable=True)
    return await _save_session_document(session_id, OPENAPI_FILENAME, render_openapi(api))
