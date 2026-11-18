"""What the delivery does with a mention of the GitHub App on its pull request: an LLM
classifies it, answers a question from the delivery's session and code as last pushed,
and the app replies in the pull request."""

import json
import tempfile
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import async_context, run_sync
from app.config import GENERATED_CODE_DIR
from app.delivery import mentions as mention_history
from app.github import checkout as repository_checkout
from app.github import client as github_client
from app.github.errors import GitHubError
from app.github.models import PullRequestMention, RepositorySelection
from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.temporal.activities import _github_token
from app.tools import repository as repository_tools
from app.tracing import langchain_config

CLASSES = ('fix', 'clarification', 'unclear')
# Bounds of what the LLM receives and what is replied. The comment itself is never cut:
# GitHub limits a comment to 65536 characters, which the webhook keeps whole, and a
# request may follow long pasted logs.
CONTEXT_CHARS = 40000
ANSWER_CHARS = 60000

CLASSIFY_PROMPT = """You sort comments that mention a code-generating GitHub App on a pull
request it opened. Answer with exactly one word:
fix - the comment asks for a change to the code, the tests or the contract of the pull
request, such as correcting a bug, adding or changing behaviour, or addressing a remark;
clarification - the comment asks a question about the pull request, its code, its tests,
its requirements or its verification, and wants an explanation, not a change;
unclear - you cannot tell which of the two the comment wants, or it wants neither.
Read the whole comment: the request may come after quoted code or pasted logs.
Do not answer the comment itself."""

ANSWER_PROMPT = """You are the GitHub App that opened this pull request for a delivery of a
generated or changed service. A reviewer mentioned you with a question. Answer it in the
pull request, in GitHub-flavoured Markdown, briefly and precisely. Base the answer on the
delivery's requirements and progress given below and on the code of the repository as it
was last pushed, which you can read with the repository tools: locate code with the
search tool and read only the relevant line ranges. Say so plainly when something cannot
be determined from them. Do not promise changes: a fix is requested by mentioning the app
with what should change. Never reveal credentials or secrets."""


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + '\n…'


def parse_classification(reply: str) -> str:
    """The classification the model answered, only when its answer is exactly one of the
    classes (allowing surrounding whitespace, quotes, emphasis and a final full stop).
    Anything else, such as an answer naming several classes or explaining itself, is
    unclear, so that an uncertain answer never starts a fix round."""
    answer = reply.strip().lower().strip('\'"`*_ \t\r\n').rstrip('.!').strip('\'"`*_ ')
    return answer if answer in CLASSES else 'unclear'


@activity.defn
async def classify_mention(session_id: str, body: str) -> str:
    """Whether the comment asks for a fix, for a clarification, or is unclear."""
    try:
        model = await get_model('review')
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [HumanMessage(content=(
                    f'{CLASSIFY_PROMPT}\n\nThe comment, in full:\n{body}'))]},
                config=langchain_config(recursion_limit=4, run_name='classify-mention'),
            )
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    choice = parse_classification(final_reply(result['messages']))
    activity.logger.info('Mention on the pull request of %s classified as %s', session_id,
                         choice)
    return choice


@activity.defn
async def answer_mention(session_id: str, body: str, context: dict[str, Any]) -> str:
    """An answer to a question about the delivery, from its session and its code as last
    pushed: a copy of the published commit of its own, never the session's working copy,
    which a running fix round or publication changes."""
    session_dir = GENERATED_CODE_DIR / session_id
    await run_sync(session_dir.mkdir, parents=True, exist_ok=True)
    async with async_context(tempfile.TemporaryDirectory, prefix='mention-',
                             dir=session_dir) as temporary:
        root = Path(temporary) / 'published'
        await run_sync(root.mkdir)
        tools = await _published_tools(session_dir, context, root)
        return await _answer(session_id, body, context, tools)


async def _published_tools(session_dir: Path, context: dict[str, Any],
                           root: Path) -> list[BaseTool]:
    """Read-only tools over the files of the delivery's last pushed commit; none when
    there is no push or the commit cannot be read."""
    publication = context.get('publication') or {}
    repository = context.get('repository')
    commit = publication.get('commit') if isinstance(publication, dict) else None
    if not isinstance(commit, str) or not isinstance(repository, dict):
        return []

    async def token() -> str:
        return (await _github_token())[0]

    try:
        await repository_checkout.snapshot(session_dir, cast(RepositorySelection, repository),
                                           commit, root, token)
    except (repository_checkout.CheckoutError, ApplicationError, OSError) as exc:
        activity.logger.warning('The pushed commit could not be read: %s', exc)
        return []
    return repository_tools.build(root)


async def _answer(session_id: str, body: str, context: dict[str, Any],
                  tools: list[BaseTool]) -> str:
    details = _bounded(json.dumps(context, default=str, ensure_ascii=False), CONTEXT_CHARS)
    try:
        model = await get_model('chat')
        graph = get_message_graph(tools, model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [
                    HumanMessage(content=(
                        f'{ANSWER_PROMPT}\n\nThe delivery, as JSON:\n{details}\n\n'
                        + ('The files of the last pushed commit are available through '
                           'the tools.' if tools
                           else 'The code of the last pushed commit is not available.')
                        + f'\n\nThe comment, in full:\n{body}')),
                ]},
                config=langchain_config(recursion_limit=30, run_name='answer-mention'),
            )
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    answer = final_reply(result['messages'])
    if not answer:
        raise ApplicationError('The model returned no answer.', non_retryable=True)
    return _bounded(answer, ANSWER_CHARS)


@activity.defn
async def record_new_mention(session_id: str, mention: PullRequestMention,
                             recording: str | None = None) -> bool:
    """Keep a mention the delivery received, with its full text, outside the workflow;
    false when the delivery already has it from another recording. A retry of the same
    recording (`recording`) after its write committed answers true again."""
    return await mention_history.add(session_id, mention, recording)


@activity.defn
async def save_mention(session_id: str, mention: PullRequestMention) -> None:
    """Keep how far a mention has come; its recorded text stays."""
    await mention_history.save(session_id, mention)


@activity.defn
async def mention_body(session_id: str, comment_type: str, comment_id: int) -> str:
    """The full text of a recorded mention, which a fix round it requested runs from."""
    text = await mention_history.body(session_id, comment_type, comment_id)
    if text is None:
        raise ApplicationError('The comment was not recorded.', non_retryable=True)
    return text


@activity.defn
async def reply_to_mention(full_name: str, number: int, comment_type: str, comment_id: int,
                           author: str, text: str) -> str:
    """Reply to the mention in pull request `number` of `full_name`, the one it was made
    on: in the thread of an inline review comment, or as a conversation comment. For an
    inline comment `comment_id` is the thread's top-level review comment, which GitHub
    takes replies to, even when the mention was a reply in that thread. Returns the
    reply's link."""
    token, _ = await _github_token()
    body = f'@{author} {text}' if comment_type == 'conversation' else text
    try:
        if comment_type == 'review':
            return await github_client.reply_to_review_comment(
                token, full_name, number, comment_id, body)
        return await github_client.comment_on_pull_request(token, full_name, number, body)
    except GitHubError as exc:
        activity.logger.warning('Could not reply in pull request #%d of %s: %s', number,
                                full_name, exc)
        raise ApplicationError(
            str(exc), non_retryable=exc.status_code in (401, 403, 404, 409, 422)) from exc
