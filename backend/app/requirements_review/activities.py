"""One round of the requirements review: the LLM checks the requirements for
contradictions between them and with the existing service's requirements."""

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.asyncio_utils import run_sync
from app.llm.graph import final_reply, get_message_graph, native_session
from app.llm.model import get_model
from app.llm.questions import normalize_questions
from app.requirements.lookup import _read_checkout_requirements, canonical_id
from app.requirements_review.models import (
    CONTRADICTION_PREFIX, MAX_CONTRADICTIONS, Contradiction, ReviewRoundInput,
    ReviewRoundResult,
)
from app.temporal.tracing import trace_activity
from app.tracing import langchain_config

# Bounds of what the reviewer receives.
SECTION_CHARS = 60000
SUMMARY_CHARS = 1000
# How often the reviewer is asked again when its answer cannot be read.
ATTEMPTS = 2

PROMPT = f"""You review the requirements of a software task for contradictions. A
contradiction is a pair (or group) of requirements that cannot all hold at once, such as
two requirements that demand incompatible behaviour, limits or technologies, or a new
requirement that conflicts with a requirement of the existing service. Gaps, vague wording,
missing detail, style and priorities are not contradictions: report only real conflicts,
and never invent one.

Check the task's requirements against each other and against the existing service's
requirements listed, if any. When you are told the contradictions of the previous round and
the user's answers, take the answers as how the user meant to resolve them, and use the
list of changes since that round to see whether they were resolved. Report a contradiction
that still holds again, with "same_as" set to its earlier ID; a resolved one is not
reported.

Report at most {MAX_CONTRADICTIONS} contradictions, the most serious first. For each one,
write the question the user is asked on how to resolve it, with 2 to 6 short, distinct
options (never an "Other" option: the user can always write their own answer).

Answer with JSON alone, no Markdown fence and no other text, in this form:
{{"contradictions": [{{"same_as": "C-1 or null", "summary": "What contradicts what, and why.",
"requirement_ids": ["FR-1", "FR-4"], "question": {{"question": "How should ...?",
"options": ["...", "..."]}}}}]}}
An empty list means the requirements are free of contradictions. Never reveal credentials
or secrets."""


def _requirement_line(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    text = " ".join(str(item.get("description", "")).split())
    return f'- {item.get("id", "")} ({item.get("priority", "")}): {item.get("title", "")} - {text}'


def _bounded(text: str) -> str:
    return text if len(text) <= SECTION_CHARS else text[:SECTION_CHARS] + "\n…"


def _section(title: str, items: list[Any]) -> str:
    lines = [line for line in (_requirement_line(item) for item in items) if line]
    return f"{title}\n" + _bounded("\n".join(lines) if lines else "(none)")


def _diff_text(diff: dict[str, Any]) -> str:
    lines = [_requirement_line(item).replace("- ", "- added: ", 1)
             for item in diff.get("added") or []]
    for change in diff.get("changed") or []:
        lines.append(_requirement_line(change.get("before")).replace("- ", "- before: ", 1))
        lines.append(_requirement_line(change.get("after")).replace("- ", "  after: ", 1))
    lines += [_requirement_line(item).replace("- ", "- removed: ", 1)
              for item in diff.get("removed") or []]
    return _bounded("\n".join(lines) if lines else "(nothing changed)")


def _previous_text(previous: list[dict[str, Any]]) -> str:
    lines = []
    for item in previous:
        question = (item.get("question") or {}).get("question", "")
        answer = item.get("answer")
        lines.append(
            f'- {item.get("id")} ({", ".join(item.get("requirement_ids") or [])}): '
            f'{item.get("summary")}\n  Question: {question}\n  '
            + (f"User's answer: {answer}" if answer else
               "Unanswered: the user dropped the question and may have changed the "
               "requirements instead."))
    return "\n".join(lines) if lines else "(none)"


def review_prompt(round_input: ReviewRoundInput, existing: list[Any]) -> str:
    parts = [PROMPT, f"Review round {round_input.round}.",
             _section("The task's requirements:", list(round_input.requirements)),
             _section("The existing service's requirements (not retired by the task):",
                      list(existing))]
    if round_input.round > 1 or round_input.previous:
        parts.append("The contradictions of the previous round:\n"
                     + _previous_text(round_input.previous))
    if round_input.diff is not None:
        parts.append("What changed in the task's requirements since the previous round:\n"
                     + _diff_text(round_input.diff))
    return "\n\n".join(parts)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _payload(text: str) -> Any:
    """The JSON object of the reviewer's answer."""
    text = _FENCE.sub("", str(text or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("The reviewer answered without a JSON object.")
    return json.loads(text[start:end + 1])


def parse_contradictions(text: str, round_input: ReviewRoundInput,
                         known_ids: set[str]) -> ReviewRoundResult:
    """The round's result from the reviewer's answer: contradictions with stable IDs (one
    reported again keeps its earlier ID, a new one gets the next number), conflicting
    requirement IDs in canonical form and known to the task, and well-formed questions.
    Raises ValueError when the answer cannot be read."""
    payload = _payload(text)
    raw = payload.get("contradictions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("The reviewer's answer has no list of contradictions.")
    earlier = {str(item.get("id")) for item in round_input.previous if item.get("id")}
    used: set[str] = set()
    number = max(1, int(round_input.next_number or 1))
    found: list[Contradiction] = []
    for item in raw:
        if len(found) >= MAX_CONTRADICTIONS:
            break
        if not isinstance(item, dict):
            continue
        summary = " ".join(str(item.get("summary") or "").split())[:SUMMARY_CHARS]
        ids = []
        for value in item.get("requirement_ids") or []:
            canonical = canonical_id(value)
            if canonical and (not known_ids or canonical in known_ids) and canonical not in ids:
                ids.append(canonical)
        try:
            question = normalize_questions([item.get("question")])[0]
        except ValueError:
            continue
        if not summary or not ids:
            continue
        same = str(item.get("same_as") or "").strip().upper()
        if same in earlier and same not in used:
            identifier = same
        else:
            identifier = f"{CONTRADICTION_PREFIX}{number}"
            number += 1
        used.add(identifier)
        found.append({"id": identifier, "summary": summary, "requirement_ids": ids,
                      "question": question})
    return ReviewRoundResult(round=round_input.round, contradictions=found,
                             next_number=number)


@activity.defn
@trace_activity('review-requirements', feature='requirements-review',
                input_parameter='round_input')
async def review_requirements(round_input: ReviewRoundInput) -> ReviewRoundResult:
    """Run one round of the review with the requirements_review model settings."""
    existing = list(round_input.existing)
    if round_input.read_existing:
        existing = list(await run_sync(_read_checkout_requirements, round_input.session_id))
    known = {canonical_id(item.get("id")) for item in [*round_input.requirements, *existing]
             if isinstance(item, dict)}
    known_ids = {item for item in known if item}
    prompt = review_prompt(round_input, existing)
    try:
        model = await get_model("requirements_review")
    except ValueError as exc:
        raise ApplicationError(f"The reviewer's model is not configured: {exc}",
                               non_retryable=True) from None
    problem = "The reviewer could not be reached."
    for attempt in range(ATTEMPTS):
        try:
            graph = get_message_graph([], model=model)
            async with native_session(model):
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content=prompt)]},
                    config=langchain_config(recursion_limit=10,
                                            run_name="review-requirements"),
                )
        except Exception as exc:  # noqa: BLE001 - retried by Temporal
            activity.logger.warning("Requirements review round %d failed: %s",
                                    round_input.round, type(exc).__name__)
            raise ApplicationError("The reviewer could not be reached.") from None
        try:
            parsed = parse_contradictions(final_reply(result["messages"]), round_input,
                                          known_ids)
        except (ValueError, TypeError, AttributeError) as exc:
            problem = f"The reviewer's answer could not be read: {exc}"
            activity.logger.warning("Requirements review round %d (attempt %d): %s",
                                    round_input.round, attempt + 1, problem)
            continue
        activity.logger.info("Requirements review round %d found %d contradictions",
                             round_input.round, len(parsed.contradictions))
        return parsed
    raise ApplicationError(problem)
