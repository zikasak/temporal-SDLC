import re
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

import psycopg
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import run_sync
from app.agent_workflows.host import session_language
from app.api_design.models import ApiDesignState, ApiModel
from app.api_design.openapi import DOCUMENT_FILENAME as OPENAPI_FILENAME
from app.api_design.openapi import api_problems, render_openapi
from app.api_design.prompt import api_turn_messages
from app.conversations.models import ConversationMessage
from app.config import GENERATED_CODE_DIR
from app.delivery.contract import (
    ContractError, RepositoryContract, read_contract, read_service_contract, write_architecture,
    write_contract,
)
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
from app.delivery.pull_request import PullRequestText
from app.delivery.workspace import SessionWorkspace
from app.delivery.pull_request import describe as pull_request_description
from app.github import checkout as repository_checkout
from app.github import client as github_client
from app.github import service as github_service
from app.github import storage as github_storage
from app.github.errors import GitHubError
from app.github.models import (
    PUSH_CONFLICT, PullRequest, RepositoryCheckout, RepositoryPublication,
    RepositorySelection,
)
from app.implementation.activities import is_service_file, published_files
from app.onboarding.models import onboarding_branch
from app.llm.graph import get_message_graph, interview, native_session
from app.llm.saved_conversations import ConversationRef
from app.llm.model import get_model
from app.llm.prompt import INLINE_DOCUMENT_CHARS, MAX_REMEMBERED_CHARS, model_messages
from app.temporal.deadline import model_request_pacer
from app.temporal.registry import register
from app.temporal.tracing import trace_activity
from app.llm.limits import AGENT_RECURSION_LIMIT
from app.tracing import langchain_config
from app.requirements.document import DOCUMENT_FILENAME, render_requirements_markdown
from app.requirements.models import Requirement, TaskKind
from app.requirements.prompt import document_messages, interview_messages
from app.review.models import COMMIT_FINDINGS, ReviewFinding, commit_findings
from app.languages.composite import suite_members
from app.routing.parts import component_suite, read_routed_parts
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
ONBOARDING_SUBJECT = "Onboard the service: contract, contract tests and Dockerfile"
ARCHITECTURE_SUBJECT = "Add the service's architecture to the contract"
# Added to the commit of an onboarding that changed the repository's existing code.
CODE_CHANGED_NOTE = ("The existing code was changed: the user confirmed that failing "
                     "contract tests revealed bugs in it.")
# Added to the commit of a change with criteria proved at seams of the code.
COMPONENTS_NOTE = ("Criteria the contract tests cannot observe are proved by the component "
                   "tests in component-tests/, at the seams whose stubs are in "
                   "contracts/components/, and, for another part P of the repository, "
                   "by those in P/component-tests/ with the stubs in "
                   "contracts/parts/P/components/.")
# Added to the commit of sources that were pushed without a check of them.
UNVERIFIED_NOTE = "Not verified: no check exists for this kind of change yet."
# Added to the commit of sources whose tests pass but whose code review left findings.
FINDINGS_NOTE = ("Not verified: the tests pass, but code review findings remained after "
                 "the last review round.")


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


def _workflow_id() -> str:
    """The workflow the running activity works for: every activity here runs in one."""
    workflow_id = activity.info().workflow_id
    assert workflow_id is not None
    return workflow_id


def _repository_tools(session_id: str | None = None) -> list[BaseTool]:
    """Read-only tools over the session's checkout; none for a session without one.

    A session is its workflow, except in Chat, where each message is its own."""
    if not activity.in_activity():
        return []
    root = repository_checkout.checkout_path(
        GENERATED_CODE_DIR, session_id or _workflow_id())
    return repository_tools.build(root) if root.is_dir() else []


def _conversation(stage: str, session_id: str | None = None,
                  restore_from: str | None = None) -> ConversationRef:
    """The saved provider conversation of the running activity's session at `stage`.
    Its tools read the session's checkout, so a checkout of another commit, such as one
    refreshed since, starts the provider conversation anew."""
    workflow_id = _workflow_id()
    try:
        commit = repository_checkout.checked_out_commit(
            GENERATED_CODE_DIR, session_id or workflow_id)
    except repository_checkout.CheckoutError:
        commit = ''
    return ConversationRef(f"{workflow_id}/{stage}",
                           f"{restore_from}/{stage}" if restore_from else None,
                           f"checkout:{commit}" if commit else "")


def _published_requirements() -> tuple[Requirement, ...]:
    """The requirements in the contract of the session's repository; none for a
    repository this pipeline has not published a service to."""
    if not activity.in_activity():
        return ()
    try:
        contract = read_service_contract(repository_checkout.checkout_path(
            GENERATED_CODE_DIR, _workflow_id()))
    except ContractError:
        return ()
    return tuple(contract["requirements"])


async def _github_token() -> tuple[str, dict[str, Any]]:
    """A fresh installation token and the saved connection it belongs to."""
    try:
        connection = await github_storage.get_connection()
        github_storage.decrypt_private_key(connection)
        assert connection is not None  # decrypting the key of no connection raised
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


def _workspace(session_id: str) -> SessionWorkspace:
    """The session's workspace on this worker."""
    return SessionWorkspace.of(GENERATED_CODE_DIR, session_id)


async def _publish(workspace: SessionWorkspace, session_id: str,
                   repository: RepositorySelection, subject: str,
                   update: Callable[[], None], verified: bool = True,
                   note: str = UNVERIFIED_NOTE, branch: str | None = None,
                   details: str = "", resolved: str | None = None) -> RepositoryPublication:
    """Apply `update` to the workspace's checkout, commit it to a new branch and push it.
    Remote commits that conflict with it fail the push with a non-retryable
    PUSH_CONFLICT error whose details are the remote tip, the conflicting files and what
    those commits changed in them; `resolved` is a remote tip whose changes the sources
    were made to take in, so its conflicts are settled as the sources have them."""
    target = workspace.checkout
    token, connection = await _github_token()
    bot = f"{connection['app_slug']}[bot]"
    try:
        await run_sync(update)
        result = await repository_checkout.push(
            target, repository, token,
            branch=branch or repository_checkout.publication_branch(session_id),
            message=(f"{subject}\n\n"
                     f"Generated from {repository['branch']} by session {session_id}."
                     + (f"\n\n{details}" if details else "")
                     + ("" if verified else f"\n\n{note}")),
            author=(bot, f"{connection['bot_user_id']}+{bot}@users.noreply.github.com"),
            verified=verified, resolved=resolved,
        )
    except repository_checkout.PushConflict as exc:
        activity.logger.warning("Could not push %s: %s", repository["full_name"], exc)
        raise ApplicationError(
            str(exc), {"commit": exc.commit, "branch": exc.branch, "files": exc.files,
             "changes": exc.changes},
            type=PUSH_CONFLICT, non_retryable=True) from exc
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
    kind: str | None = None, findings: list[ReviewFinding] | None = None,
    components: dict[str, Any] | None = None, resolved: str | None = None,
) -> RepositoryPublication:
    """Activity: `publish_repository_from` the session's workspace on this worker."""
    return await publish_repository_from(_workspace(session_id), session_id, repository,
                                         contract, kind, findings, components, resolved)


async def publish_repository_from(
    workspace: SessionWorkspace, session_id: str, repository: RepositorySelection,
    contract: RepositoryContract, kind: str | None = None,
    findings: list[ReviewFinding] | None = None, components: dict[str, Any] | None = None,
    resolved: str | None = None,
) -> RepositoryPublication:
    """Commit the verified service, its contract tests and the contract they were built
    from to a new branch and push it. With open review `findings` the tests pass but the
    code review was not satisfied: the commit is marked as unverified and lists them.

    A change with criteria proved at seams of the code passes `components`: its component
    tests replace the repository's `component-tests/`, and the component contract its
    routing checked replaces `contracts/components/`; those of another part P with routes,
    read from the routing, replace `P/component-tests/` and `contracts/parts/P/components/`.
    Its contract tests replace the
    repository's only when an author of the change wrote them (`contract_tests`); otherwise
    the repository's suite, which the change was verified to keep passing, stays.

    Reads the workspace's checkout, service, contract tests and, with `components`, its
    component tests; the contract written is `contract`."""
    registered = session_language(contract['language'])
    target, service, suite = workspace.checkout, workspace.service, workspace.suite
    # The unit tests too, when the service's author maintained them.
    service_files = await run_sync(published_files, registered, service)
    contract_tests = components is None or bool(components.get('contract_tests'))
    sources = [target, service, *([suite] if contract_tests else [])]
    # Each part with routes: its component tests, where the suite has them and where the
    # repository keeps them, and its stubs, as the routing kept and the repository
    # publishes them.
    pushed: list[tuple[Path, Path, frozenset[Path], Path, Path]] = []
    if components is not None:
        tests = workspace.component_tests
        try:
            parts = await run_sync(read_routed_parts, registered, workspace.root)
            components_suite = component_suite(registered, parts)
        except ValueError as exc:
            raise ApplicationError(f"The component tests cannot be pushed: {exc}",
                                   non_retryable=True) from exc
        by_name = {part.key: part for part in parts}
        for member in suite_members(components_suite):
            part = by_name[member.name]
            pushed.append((tests / member.directory, target / part.tests_dir(),
                           frozenset(Path(item) for item in member.suite.published),
                           part.routed_contract(workspace.root), target / part.published_dir()))
        sources += [tests, *(stubs for *_, stubs, _ in pushed)]
    if not await run_sync(all, (item.is_dir() for item in sources)):
        raise ApplicationError("The verified sources or the repository checkout are unavailable.",
                               non_retryable=True)

    def update() -> None:
        repository_checkout.replace_files(service, target, service_files)
        if contract_tests:
            repository_checkout.replace_files(suite, target / repository_checkout.TESTS_DIR,
                                              lambda relative: True)
        write_contract(target, contract)
        for own, destination, published, stubs, stubs_destination in pushed:
            # Only what the part's suite publishes: any other file the repository keeps
            # beside it was never shown to the author, as with the service's files.
            repository_checkout.replace_files(
                own, destination,
                lambda relative, published=published: relative in published
                or not published.isdisjoint(relative.parents))
            # The routing kept every published stub and only extended them.
            repository_checkout.replace_files(stubs, stubs_destination, lambda relative: True)

    subject = COMMIT_SUBJECTS.get(kind or '', COMMIT_SUBJECT)
    details = COMPONENTS_NOTE if components is not None else ""
    if not findings:
        result = await _publish(workspace, session_id, repository, subject, update,
                                details=details, resolved=resolved)
        return {**result, 'findings': [], 'findings_count': 0}
    result = await _publish(
        workspace, session_id, repository, f"{subject} (unverified)", update, verified=False,
        note=f"{FINDINGS_NOTE}\n\n{commit_findings(findings)}", details=details,
        resolved=resolved)
    return {**result, 'findings': cast(list[dict[str, Any]], findings[:COMMIT_FINDINGS]),
            'findings_count': len(findings)}


@activity.defn
@trace_activity('publish-onboarding', feature='repository', input_parameter='repository')
async def publish_onboarding(
    session_id: str, repository: RepositorySelection, contract: RepositoryContract,
    findings: list[ReviewFinding] | None = None, code_changed: bool = False,
    resolved: str | None = None,
) -> RepositoryPublication:
    """Activity: `publish_onboarding_from` the session's workspace on this worker."""
    return await publish_onboarding_from(_workspace(session_id), session_id, repository,
                                         contract, findings, code_changed, resolved)


async def publish_onboarding_from(
    workspace: SessionWorkspace, session_id: str, repository: RepositorySelection,
    contract: RepositoryContract, findings: list[ReviewFinding] | None = None,
    code_changed: bool = False, resolved: str | None = None,
) -> RepositoryPublication:
    """Commit an onboarding's verified result to its own new branch and push it: the
    contract drafted from the existing code, the contract tests that replace the
    repository's, the root Dockerfile and, where the user confirmed bugs, the existing
    code as it was fixed. Open review `findings` mark the commit as unverified.

    Reads the workspace's checkout, service and contract tests; the contract written is
    `contract`."""
    service_files = partial(is_service_file, session_language(contract['language']))
    target, service, suite = workspace.checkout, workspace.service, workspace.suite
    if not await run_sync(all, (item.is_dir() for item in (target, service, suite))):
        raise ApplicationError("The verified sources or the repository checkout are unavailable.",
                               non_retryable=True)

    def update() -> None:
        repository_checkout.replace_files(service, target, service_files)
        repository_checkout.replace_files(suite, target / repository_checkout.TESTS_DIR,
                                          lambda relative: True)
        write_contract(target, contract)

    branch = onboarding_branch(repository['branch'], session_id)
    details = CODE_CHANGED_NOTE if code_changed else ""
    if not findings:
        result = await _publish(workspace, session_id, repository, ONBOARDING_SUBJECT,
                                update, branch=branch, details=details, resolved=resolved)
        return {**result, 'findings': [], 'findings_count': 0}
    result = await _publish(
        workspace, session_id, repository, f"{ONBOARDING_SUBJECT} (unverified)", update, verified=False,
        note=f"{FINDINGS_NOTE}\n\n{commit_findings(findings)}", branch=branch, details=details,
        resolved=resolved)
    return {**result, 'findings': cast(list[dict[str, Any]], findings[:COMMIT_FINDINGS]),
            'findings_count': len(findings)}


@activity.defn
@trace_activity('publish-onboarding-architecture', feature='repository',
                input_parameter='repository')
async def publish_onboarding_architecture(
    session_id: str, repository: RepositorySelection, architecture: str,
    structure: dict[str, Any] | None,
) -> RepositoryPublication:
    """Commit the architecture an onboarding drafted to its own new branch and push it:
    contracts/architecture.md and contracts/architecture.json, and nothing else."""
    workspace = _workspace(session_id)
    target = workspace.checkout
    if not await run_sync(target.is_dir):
        raise ApplicationError("The repository checkout is unavailable.", non_retryable=True)
    if not architecture:
        raise ApplicationError("The onboarding has no architecture to push.",
                               non_retryable=True)
    result = await _publish(
        workspace, session_id, repository, ARCHITECTURE_SUBJECT,
        lambda: write_architecture(target, architecture, structure),
        branch=onboarding_branch(repository['branch'], session_id))
    return {**result, 'findings': [], 'findings_count': 0}


@activity.defn
@trace_activity('publish-documentation', feature='repository', input_parameter='repository')
async def publish_documentation(
    session_id: str, repository: RepositorySelection, language: str, verified: bool,
    resolved: str | None = None,
) -> RepositoryPublication:
    """Activity: `publish_documentation_from` the session's workspace on this worker."""
    return await publish_documentation_from(_workspace(session_id), session_id, repository,
                                            language, verified, resolved)


async def publish_documentation_from(
    workspace: SessionWorkspace, session_id: str, repository: RepositorySelection,
    language: str, verified: bool, resolved: str | None = None,
) -> RepositoryPublication:
    """Commit the documentation its author changed to a new branch and push it. Nothing
    else of the repository changes: not the service, its tests or its contract. Without
    `verified` the commit and the publication say that nothing checked the change.

    Reads the workspace's checkout and the documentation its author wrote."""
    documentation_files = partial(is_documentation_file, session_language(language))
    target, written = workspace.checkout, workspace.documentation
    if not await run_sync(all, (item.is_dir() for item in (target, written))):
        raise ApplicationError("The documentation or the repository checkout is unavailable.",
                               non_retryable=True)
    return await _publish(
        workspace, session_id, repository,
        DOCUMENTATION_SUBJECT if verified else f"{DOCUMENTATION_SUBJECT} (unverified)",
        lambda: repository_checkout.replace_files(written, target, documentation_files),
        verified, resolved=resolved)


@activity.defn
@trace_activity('open-pull-request', feature='repository', input_parameter='repository')
async def open_pull_request(
    session_id: str, repository: RepositorySelection, publication: RepositoryPublication,
    change: dict[str, Any], text: PullRequestText | None = None,
) -> PullRequest:
    """Open a pull request from the pushed branch to the repository's default branch, or
    update the open one of that branch, rewriting its title and description: the `text`
    the LLM wrote of what the delivery changed and why, or without it the generic text of
    the task kind, followed by the open review findings when there are any. An unverified
    delivery opens a draft; a verified push marks a draft ready for review."""
    kind = change.get('kind')
    subject = (DOCUMENTATION_SUBJECT if kind == 'docs'
               else ONBOARDING_SUBJECT if kind == 'onboarding'
               else ARCHITECTURE_SUBJECT if kind == 'onboarding_architecture'
               else COMMIT_SUBJECTS.get(kind or '', COMMIT_SUBJECT))
    title, body = pull_request_description(subject, change, publication, text)
    verified = bool(publication['verified'])
    token, _ = await _github_token()
    full_name, branch = repository['full_name'], publication['branch']
    try:
        base = await github_client.get_default_branch(token, full_name)
        pull = await github_client.find_open_pull_request(token, full_name, branch, base)
        if pull:
            pull = await github_client.update_pull_request(
                token, full_name, pull['number'], title, body)
            if verified and pull['draft']:
                pull['draft'] = await github_client.set_pull_request_draft(
                    token, pull['node_id'], False)
            action = 'Updated'
        else:
            pull = await github_client.create_pull_request(
                token, full_name, branch, base, title, body, draft=not verified)
            action = 'Opened'
        # Mentions of the app on the pull request find their delivery by it.
        await github_storage.record_pull_request(full_name, pull['number'], session_id)
    except GitHubError as exc:
        activity.logger.warning("Could not open or update the pull request of %s: %s",
                                branch, exc)
        # Missing permissions, a removed installation or a rejected request need a person.
        raise ApplicationError(
            str(exc), type='PullRequestError',
            non_retryable=exc.status_code in (401, 403, 404, 409, 422)) from exc
    activity.logger.info("%s pull request #%d of %s", action, pull['number'], branch)
    return cast(PullRequest, {key: pull[key] for key in ('number', 'title', 'url', 'draft', 'base')})


@activity.defn
async def pull_request_state(full_name: str, number: int) -> dict[str, Any]:
    """Activity 'pull_request_state': the state of pull request `number` of the
    repository `full_name` ('owner/name') on GitHub, as {'state': 'open' or 'closed',
    'merged': bool}. Run by a waiting delivery every PULL_REQUEST_CHECK_INTERVAL."""
    token, _ = await _github_token()
    try:
        current = await github_client.get_pull_request_state(token, full_name, number)
    except GitHubError as exc:
        activity.logger.warning("Could not read pull request #%s of %s: %s",
                                number, full_name, exc)
        # The waiting delivery checks again at its next interval.
        raise ApplicationError(str(exc), type='PullRequestError',
                               non_retryable=True) from exc
    if current is None:
        # GitHub answers 404 also when the installation lost access to the repository or
        # the repository moved: that says nothing about the pull request, so the check
        # fails and the waiting delivery checks again at its next interval.
        activity.logger.warning("Pull request #%s of %s not found; state unknown",
                                number, full_name)
        raise ApplicationError(
            f"Pull request #{number} of {full_name} was not found.",
            type='PullRequestError', non_retryable=True)
    return {'state': 'closed' if current['state'] == 'closed' else 'open',
            'merged': bool(current['merged'])}


@activity.defn
@trace_activity('read-repository-contract', feature='repository', input_parameter='session_id')
async def read_repository_contract(session_id: str) -> RepositoryContract:
    """The contract the session's repository was published with, which a task that
    changes its service starts from."""
    target = repository_checkout.checkout_path(GENERATED_CODE_DIR, session_id)
    try:
        return await run_sync(read_contract, target)
    except ContractError as exc:
        activity.logger.warning("Session %s has no usable contract: %s", session_id, exc)
        # Shown to the user as it is; another attempt reads the same checkout.
        raise ApplicationError(str(exc), type='ContractError', non_retryable=True) from exc


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
    parent_request_id: str | None = None,
) -> str:
    """Each request of a chat is its own workflow and saves its provider conversation as
    itself; it continues the one of the request it answers (`parent_request_id`), so the
    branches continued from one request stay apart."""

    # Older workflows can still have a single string as their activity input.
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    session_id = session_id or _workflow_id()
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
    repository_files = await run_sync(_repository_tools, session_id) if repository else []
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
        model = await get_model('chat')
        graph = get_message_graph(
            [*build_tools(ToolContext(session_id, document_ids)), *repository_files],
            model=model, after_model_request=model_request_pacer())
        async with native_session(
                model, _conversation("chat", session_id, parent_request_id or None)):
            result = await graph.ainvoke(
                {"messages": prompt},
                config=langchain_config(recursion_limit=AGENT_RECURSION_LIMIT, run_name='respond-with-tools'),
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
    # The workflow hands over its state's plain records.
    conversation = cast(list[ConversationMessage], messages)
    gathered = cast(list[Requirement], requirements)
    task_kind = cast(TaskKind | None, kind)
    ended = cast(list[Requirement], retired or [])
    ctx = requirements_tools.RequirementsContext.from_list(
        gathered, task_kind, await run_sync(_published_requirements), ended)
    repository = await run_sync(_repository_tools)
    asked = question_tools.QuestionsContext()
    started = time.perf_counter()
    try:
        model = await get_model('requirements')
        graph = get_message_graph(
            [*requirements_tools.build(ctx), *question_tools.build(asked), *repository],
            until=lambda: bool(asked.questions), model=model)
        async with native_session(model, _conversation("requirements")):
            reply = await interview(
                graph, interview_messages(
                    conversation, gathered, task_kind,
                    repository_tools.GUIDANCE if repository else "",
                    list(ctx.service_requirements()), ctx.retired_list()),
                langchain_config(recursion_limit=AGENT_RECURSION_LIMIT, run_name='interview-with-tools'),
                lambda: bool(asked.questions))
    except ValueError as exc:
        activity.logger.warning("Requirements turn failed: %s", exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    updated = ctx.as_list()
    activity.logger.info("Requirements turn took %.1fs: %d -> %d requirements",
                         time.perf_counter() - started, len(requirements), len(updated))
    if not reply and not asked.questions:
        if updated == requirements and ctx.kind == kind and ctx.retired_list() == ended:
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
    contradictions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The requirements document; `contradictions` are those the user continued despite."""
    try:
        model = await get_model('requirements')
        answer = await model.ainvoke(
            document_messages(cast(list[ConversationMessage], messages),
                              cast(list[Requirement], requirements),
                              cast(list[Requirement] | None, retired)),
            config=langchain_config(run_name='write-requirements-overview'),
        )
        narrative = str(answer.text).strip() or None
    except Exception:
        # The requirement tables alone are still a useful document.
        activity.logger.exception("Could not write the requirements overview")
        narrative = None
    content = render_requirements_markdown(cast(list[Requirement], requirements), narrative,
                                           cast(list[Requirement] | None, retired),
                                           contradictions)
    return await _save_session_document(session_id, DOCUMENT_FILENAME, content)


@activity.defn
async def read_requirements_document(session_id: str, document_id: str) -> str:
    """The text of the session's requirements document, as the user downloads it."""
    try:
        return (await get_generated_document(document_id, session_id))["parsed_text"]
    except DocumentNotFound as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn
async def register_session(session_id: str) -> None:
    """Record a session a workflow started, as the API records the ones it starts."""
    await register(session_id)


@activity.defn
@trace_activity('design-api', feature='api-design', as_type='agent')
async def api_design_turn(state: ApiDesignState) -> dict[str, Any]:
    ctx = api_design_tools.ApiDesignContext.from_model(
        state.api, {item["id"] for item in state.requirements}, state.language,
        language_fixed=bool(state.change_document),
    )
    repository = await run_sync(_repository_tools)
    asked = question_tools.QuestionsContext()
    started = time.perf_counter()
    try:
        model = await get_model('api_design')
        graph = get_message_graph(
            [*api_design_tools.build(ctx), *question_tools.build(asked), *repository],
            until=lambda: bool(asked.questions), model=model)
        async with native_session(model, _conversation("api-design")):
            reply = await interview(
                graph, api_turn_messages(state, repository_tools.GUIDANCE if repository else ""),
                langchain_config(recursion_limit=AGENT_RECURSION_LIMIT, run_name='draft-api-with-tools'),
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
