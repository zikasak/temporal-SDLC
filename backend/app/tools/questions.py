"""The ask_user tool of the interviewing models. A call ends the turn: its questions are
shown with the reply, and the user's answers arrive as their next message.

Not part of the chat TOOLSETS: the interview activities build it themselves.
"""

import logging
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, StructuredTool

from app.llm.questions import ASK_USER, DESCRIPTION, AskUserInput, Question, normalize_questions

logger = logging.getLogger(__name__)

GUIDANCE = (
    "\nYou can ask the user questions only with the ask_user tool: the page shows each one "
    "with its options and a field for an answer of the user's own. Never ask a question "
    "in your reply text, not even a rhetorical one; the reply only reports what you did "
    "and explains. Make the other tool calls of the turn first, then call ask_user once "
    "with every question of the turn and a short reply. The turn ends with that call; "
    "the answers arrive as the user's next message."
)


@dataclass
class QuestionsContext:
    """The questions one turn asked, for the workflow to store with its reply."""

    questions: list[Question] = field(default_factory=list)


def build(ctx: QuestionsContext) -> list[BaseTool]:
    def ask_user(questions: list) -> str:
        if ctx.questions:
            return "Questions not asked: this turn already asked its questions."
        try:
            ctx.questions = normalize_questions(questions)
        except ValueError as exc:
            logger.warning("Questions not asked: %s", exc)
            return f"Questions not asked: {exc}"
        return ("The questions are shown to the user. End the turn now; their answers "
                "arrive as the next message.")

    return [StructuredTool.from_function(
        ask_user, name=ASK_USER, description=DESCRIPTION, args_schema=AskUserInput)]
