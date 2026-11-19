"""The conversation every session workflow holds with its user."""
import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.conversations.models import ConversationMessage
    from app.llm.questions import Question, answer_problem, answers_text

MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 10000


def pending_questions(messages: Sequence[Mapping[str, Any]]) -> list[Question]:
    """The questions the last message asked with ask_user, which the next must answer."""
    if not messages or messages[-1]["role"] != "assistant":
        return []
    return messages[-1].get("questions") or []


def check_message(messages: Sequence[Mapping[str, Any]], text: str, too_long: str) -> None:
    """Raise ValueError unless `text` can be the session's next user message."""
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise ValueError(f"A message must contain 1 to {MAX_MESSAGE_CHARS} characters.")
    encoded = json.dumps(
        [*messages, {"role": "user", "content": text}],
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
        raise ValueError(too_long)


def check_answers(messages: Sequence[Mapping[str, Any]], answers: list[str],
                  too_long: str) -> None:
    """Raise ValueError unless `answers` answer the questions the session waits for."""
    questions = pending_questions(messages)
    if not questions:
        raise ValueError("There are no questions to answer.")
    problem = answer_problem(questions, answers)
    if problem:
        raise ValueError(problem)
    check_message(messages, answers_text(questions, answers), too_long)


def assistant_message(result: dict[str, Any]) -> ConversationMessage:
    """The message a turn added: its reply and the questions it asked."""
    message: ConversationMessage = {"role": "assistant", "content": result["reply"]}
    if result.get("questions"):
        message["questions"] = result["questions"]
    return message


class InterviewState(Protocol):
    """What an interviewing session's state holds for Interview."""

    messages: list[ConversationMessage]

    @property
    def status(self) -> str: ...


StateT = TypeVar('StateT', bound=InterviewState)


class Interview(Generic[StateT]):
    """The user's side of a session's conversation: a message, or answers to the questions
    of the last message, each an update that runs one LLM turn. The workflow holds
    `state` with its `status` and `messages`, counts its updates in `_updates`, queues
    turns on `_turn_lock`, and defines `_turn` and `_check_open`, which raises ValueError
    unless the session waits for the user."""

    state: StateT
    _updates: int
    _turn_lock: asyncio.Lock

    if TYPE_CHECKING:
        async def _turn(self) -> bool: ...

        def _check_open(self) -> None: ...

    # Who asks the questions, as the refusal of a message that skips them names it.
    INTERVIEWER = "interviewer"
    # The refusals of a message that makes the transcript too long, and of one to a
    # session that ended while the message waited for its turn.
    TOO_LONG = "This conversation is too long to continue."
    CLOSED = "This session is no longer accepting messages."

    def _questions_open(self) -> bool:
        """Whether the last message asked questions the user has to answer first."""
        return bool(pending_questions(self.state.messages))

    def _user_replies(self) -> None:
        """Called before the user's message or answers are added to the conversation."""

    @workflow.update
    async def send_message(self, message: str) -> None:
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError(self.CLOSED)
            workflow.logger.info("Message received (%d characters)", len(message))
            self._user_replies()
            self.state.messages.append({"role": "user", "content": message.strip()})
            await self._turn()

    @send_message.validator
    def validate_send_message(self, message: str) -> None:
        self._check_open()
        if self._questions_open():
            raise ValueError(f"Answer the {self.INTERVIEWER}'s questions first.")
        check_message(self.state.messages, message.strip(), self.TOO_LONG)

    @workflow.update
    async def answer_questions(self, answers: list[str]) -> None:
        """The user's answers to the questions of the last message, as their message."""
        self._updates += 1
        async with self._turn_lock:
            if self.state.status != "interviewing":
                raise ApplicationError(self.CLOSED)
            questions = pending_questions(self.state.messages)
            workflow.logger.info("Answers received (%d questions)", len(questions))
            self._user_replies()
            self.state.messages.append(
                {"role": "user", "content": answers_text(questions, answers)})
            await self._turn()

    @answer_questions.validator
    def validate_answer_questions(self, answers: list[str]) -> None:
        self._check_open()
        check_answers(self.state.messages, answers, self.TOO_LONG)
