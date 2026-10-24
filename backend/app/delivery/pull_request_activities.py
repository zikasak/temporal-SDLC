"""The LLM writes the title and the description of a delivery's pull request from the
task, its requirements and the delivery's code diff. Its failure never fails the pull
request step: the workflow then opens or updates the pull request with the generic text
and reports why (pull_request_text_error)."""

import json
from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.config import GENERATED_CODE_DIR
from app.delivery.pull_request import TITLE_CHARS, PullRequestText, parse_text
from app.github import checkout as repository_checkout
from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.tracing import langchain_config

# Bounds of what the LLM receives.
TASK_CHARS = 12000
REQUIREMENTS_CHARS = 12000
DIFF_CHARS = 60000
# The error type of this activity's failures whose messages are safe to show in the
# delivery progress; any other failure is reported without its message.
SAFE_ERROR = 'PullRequestTextError'

KIND_NAMES = {'new_service': 'a new service', 'api_change': 'a change of the API',
              'bugfix': 'a bug fix', 'docs': 'a documentation change',
              'onboarding': ('an onboarding of an existing service: its contract drafted '
                             'from the code, contract tests and a root Dockerfile')}

PROMPT = f"""You write the title and the description of a GitHub pull request that a
code-generating pipeline opens for its delivery. Base them on the task, its requirements
and the delivery's code diff below.

Answer in exactly this form and nothing else:
- the first line is the title: at most {TITLE_CHARS} characters, plain text, naming the
  specific change (for example "Add rate limiting to sign-in" rather than "Fix the
  service"); no Markdown, no quotes, no "Title:" label;
- then an empty line;
- then the description in GitHub-flavoured Markdown: a short summary of what the
  delivery changed and the purpose of the change, from the reader's point of view.

Do not list the requirements one by one, do not describe the verification status, the
branch or the commit, and do not list review findings: those are added separately. Do
not invent changes the diff does not show. Never reveal credentials or secrets."""


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + '\n…'


def _requirements(items: list[dict[str, Any]], heading: str) -> str:
    if not items:
        return ''
    lines = [f'{heading}:']
    for item in items:
        lines.append(f'- {item.get("id", "")}: {item.get("title", "")} - '
                     f'{" ".join(str(item.get("description", "")).split())}')
    return _bounded('\n'.join(lines), REQUIREMENTS_CHARS)


@activity.defn
async def write_pull_request_text(session_id: str, publication: dict[str, Any],
                                  change: dict[str, Any]) -> PullRequestText:
    """The title and the summary of the pull request of `publication`, written by the
    LLM. `change` holds the task's kind, its document (`task`), the requirements it
    delivered and those it retired."""
    session_dir = GENERATED_CODE_DIR / session_id
    try:
        diff = await repository_checkout.delivery_diff(
            session_dir, str(publication.get('commit') or ''), DIFF_CHARS)
    except (repository_checkout.CheckoutError, OSError) as exc:
        activity.logger.warning('The diff of session %s could not be read: %s', session_id,
                                exc)
        raise ApplicationError("The delivery's code diff could not be read.",
                               type=SAFE_ERROR, non_retryable=True) from exc
    kind = change.get('kind') or 'new_service'
    parts = [
        PROMPT,
        f'The task is {KIND_NAMES.get(kind, KIND_NAMES["new_service"])}.',
        ('The existing code was changed to fix bugs the user confirmed; say so in the '
         'description.' if change.get('existing_code_changed') else ''),
        'The task, as its requirements document states it:\n'
        + _bounded(str(change.get('task') or '(not available)'), TASK_CHARS),
        _requirements(list(change.get('requirements') or []), 'Requirements delivered'),
        _requirements(list(change.get('retired') or []), 'Requirements retired'),
        'The delivery\'s code diff:\n' + (diff or '(empty)'),
    ]
    try:
        model = await get_model('review')
        graph = get_message_graph([], model=model)
        async with native_session(model):
            result = await graph.ainvoke(
                {'messages': [HumanMessage(content='\n\n'.join(part for part in parts if part))]},
                config=langchain_config(recursion_limit=4, run_name='write-pull-request-text'),
            )
    except ValueError as exc:
        # The provider's own message may carry credentials; it is only logged.
        activity.logger.warning('The LLM could not write the pull request text of session '
                                '%s: %s', session_id, type(exc).__name__)
        raise ApplicationError('The LLM is not configured or rejected the request.',
                               type=SAFE_ERROR, non_retryable=True) from None
    except Exception as exc:
        activity.logger.warning('The LLM could not write the pull request text of session '
                                '%s: %s', session_id, type(exc).__name__)
        raise ApplicationError('The LLM could not be reached or failed to answer.',
                               type=SAFE_ERROR) from None
    try:
        text = parse_text(final_reply(result['messages']))
    except ValueError as exc:
        # parse_text's messages are fixed sentences.
        raise ApplicationError(str(exc), type=SAFE_ERROR, non_retryable=True) from exc
    activity.logger.info('Pull request text of session %s written: %s', session_id,
                         json.dumps(text['title'], ensure_ascii=False))
    return text
