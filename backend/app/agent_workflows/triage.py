"""Host-only read-only pre-triage for coding/test agent `ask_user` questions.

Answers questions settled by the baseline repository checkout, contracts, or stubs so
the user is only interrupted for genuine product or requirement decisions, while keeping
test and implementation workspaces strictly isolated from one another.
"""
import asyncio
import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from app.agent_workflows.runtime import workspace_tools
from app.asyncio_utils import run_sync
from app.llm.graph import get_message_graph, native_session
from app.llm.model import get_model
from app.llm.questions import Question, answer_problem
from app.settings.models import WorkflowType
from app.tools import repository as repository_tools
from app.tracing import langchain_config

logger = logging.getLogger(__name__)

TRIAGE_ROLES = frozenset({'implementation', 'test_generation', 'component_test_generation'})
MAX_TRIAGE_ROUNDS = 2
TRIAGE_SECONDS = 60
TRIAGE_RECURSION_LIMIT = 24

_TRIAGE_PROMPT = """You triage questions asked with `ask_user` by an isolated `{role}` agent.
The user is a product owner who does not know internal codebase wiring, directory layouts,
helper names, or stub mechanics. Your job is to answer questions that are already settled
by the baseline repository, the contracts/stubs, or the brief/requirements, so the user is
not interrupted needlessly.

Tools:
- `repo_*` tools (when present) read the baseline repository checkout (read-only).
- `stub_*` / `part_stub_*` tools (when present) read the routed component/page contracts (read-only).
You never have access to the other agent's generated tests or implementation changes.

Rules:
1. Inspect the relevant baseline files/contracts with tools before deciding. Never guess.
2. Answer ONLY when **every** question is definitively settled without inventing product
   behaviour. If even one question requires a genuine product/requirement decision from the
   user, return `{{"answerable": false, "reason": "..."}}`.
3. Maintain strict separation:
   - If `{role}` is a test author (`test_generation` or `component_test_generation`):
     never reveal unstubbed internal implementation logic. If the question asks how an
     existing stubbed symbol finds a checkout/workspace/collaborator, explain only what is
     in the stubs or standard test harness conventions (e.g., `SessionWorkspace` /
     `<GENERATED_CODE_DIR>/<session_id>/repository`, monkeypatching stubbed constants,
     or registering fake Temporal activities/workflows by the stub's registered name). If
     the test needs an unstubbed collaborator or setup precondition the stubs omitted,
     answer by instructing the test author to decline the criterion in `unobservable.json`
     with `"kind": "contract_gap"` naming what the stubs lack so routing can extend them.
   - If `{role}` is `implementation`: answer questions settled by existing codebase
     conventions, helper patterns, contracts, or stubs; never reference tests.
4. Reply with ONLY valid JSON (no markdown fences):
   `{{"answerable": true, "answers": ["...", ...]}}` (one non-empty string per question,
   in the exact order asked) or `{{"answerable": false, "reason": "..."}}`.
"""

_JSON_BLOCK = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.DOTALL)


def _prefixed_tools(tools: list[BaseTool], prefix: str) -> list[BaseTool]:
    """Rename tools cleanly so repo and contract tools do not collide in one graph."""
    renamed: list[BaseTool] = []
    for item in tools:
        renamed.append(item.model_copy(update={'name': f'{prefix}_{item.name}'}))
    return renamed


def _build_triage_tools(session_dir: Path) -> list[BaseTool]:
    """Read-only tools over the baseline checkout and routed contracts, never stage outputs."""
    from app.github.checkout import CHECKOUT_DIR, COMPONENTS_DIR
    from app.routing.parts import PARTS_DIR

    tools: list[BaseTool] = []
    checkout = session_dir / CHECKOUT_DIR
    if checkout.is_dir():
        tools.extend(_prefixed_tools(repository_tools.build(checkout), 'repo'))
    routing = session_dir / 'routing'
    for subdir, prefix in ((COMPONENTS_DIR, 'stub'), (PARTS_DIR, 'part_stub')):
        target = routing / subdir
        if target.is_dir():
            stub_tools = [
                item for item in workspace_tools(target)
                if item.name in repository_tools.READ_ONLY
            ]
            tools.extend(_prefixed_tools(stub_tools, prefix))
    return tools


def _parse_triage_reply(text: str, questions: Sequence[Question]) -> list[str] | None:
    raw = (text or '').strip()
    match = _JSON_BLOCK.search(raw)
    if match:
        raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get('answerable') is not True:
        return None
    answers = data.get('answers')
    if answer_problem(questions, answers) is not None:
        return None
    assert isinstance(answers, list)
    return [str(item).strip() for item in answers]


def _has_triage_sources(session_dir: Path, context: dict[str, Any]) -> bool:
    from app.github.checkout import CHECKOUT_DIR, COMPONENTS_DIR
    from app.routing.parts import PARTS_DIR

    if (session_dir / CHECKOUT_DIR).is_dir():
        return True
    routing = session_dir / 'routing'
    if (routing / COMPONENTS_DIR).is_dir() or (routing / PARTS_DIR).is_dir():
        return True
    return bool(context.get('brief') or context.get('requirements') or context.get('api_design'))


async def _load_triage_model(role: str) -> Any:
    try:
        return await get_model('routing')
    except Exception:
        return await get_model(cast(WorkflowType, role))


async def triage_questions(
    role: str, session_dir: Path, context: dict[str, Any], questions: Sequence[Question],
) -> list[str] | None:
    """Return auto-answers for `questions` when settled by the baseline repo/contracts,
    or None when the user should answer (or when triage cannot run)."""
    if role not in TRIAGE_ROLES or not questions or not _has_triage_sources(session_dir, context):
        return None
    try:
        tools = await run_sync(_build_triage_tools, session_dir)
        model = await _load_triage_model(role)
        payload = json.dumps({
            'role': role,
            'questions': list(questions),
            'stage_context': {
                key: value for key, value in context.items()
                if key in ('brief', 'requirements', 'routes', 'component_routes', 'repository',
                           'onboarding', 'api_design')
            },
        }, ensure_ascii=False)
        messages = [
            SystemMessage(content=_TRIAGE_PROMPT.format(role=role)),
            HumanMessage(content=payload),
        ]
        config = langchain_config(run_name='triage-agent-questions')
        graph_config = {**config, 'recursion_limit': TRIAGE_RECURSION_LIMIT}
        async with asyncio.timeout(TRIAGE_SECONDS):
            async with native_session(model):
                if tools:
                    graph = get_message_graph(tools, model=model)
                    result = await graph.ainvoke({'messages': messages}, config=graph_config)
                    reply = result['messages'][-1]
                else:
                    reply = await model.ainvoke(messages, config=config)
        text = str(getattr(reply, 'text', None) or getattr(reply, 'content', '') or '')
        return _parse_triage_reply(text, questions)
    except Exception as exc:
        logger.warning('Pre-triage for %s questions fell back to user: %s', role, exc)
        return None
