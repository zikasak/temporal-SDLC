"""The ask_user tool: questions the model puts to the user, each with suggested answers.
The user picks one of them or writes their own. Shared by the host and the agent images,
so it depends on nothing beyond pydantic."""
import json
import re
from collections.abc import Sequence
from typing import Any, TypedDict

from pydantic import BaseModel, Field

ASK_USER = 'ask_user'
MAX_QUESTIONS = 5
MIN_OPTIONS = 2
MAX_OPTIONS = 6
QUESTION_CHARS = 500
OPTION_CHARS = 200
ANSWER_CHARS = 2000
# What a free answer's choice is called in the page; the model never offers it itself.
OTHER = 'Other'

DESCRIPTION = (
    'Ask the user one or more questions and wait for the answers. This is the only way to '
    'ask the user anything. Give each question 2 to 6 short, distinct options covering the '
    'likely answers; the user picks one or writes their own answer instead, so never add an '
    '"Other" option. Put every question you have now into one call.'
)


class Question(TypedDict):
    question: str
    options: list[str]


class QuestionInput(BaseModel):
    question: str = Field(description='One clear question, ending with a question mark.')
    options: list[str] = Field(
        description=f'{MIN_OPTIONS} to {MAX_OPTIONS} suggested answers, most likely first.')


class AskUserInput(BaseModel):
    questions: list[QuestionInput] = Field(
        description=f'1 to {MAX_QUESTIONS} questions, asked together.')


def normalize_questions(raw: Any) -> list[Question]:
    """Bounded, well-formed questions from a tool call or a stored record; raises
    ValueError with what to fix."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        raise ValueError(f'ask between 1 and {MAX_QUESTIONS} questions.')
    questions: list[Question] = []
    for number, item in enumerate(raw, 1):
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            raise ValueError(f'question {number} must have a question and options.')
        text = str(item.get('question') or '').strip()
        if not text or len(text) > QUESTION_CHARS:
            raise ValueError(f'question {number} must have 1 to {QUESTION_CHARS} characters.')
        options = [str(option).strip() for option in item.get('options') or []]
        options = [option for option in dict.fromkeys(options) if option]
        if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            raise ValueError(f'question {number} needs {MIN_OPTIONS} to {MAX_OPTIONS} distinct '
                             'options.')
        if any(len(option) > OPTION_CHARS for option in options):
            raise ValueError(f'options of question {number} must have at most {OPTION_CHARS} '
                             'characters.')
        if any(option.lower().rstrip(':.') in {'other', 'something else'} for option in options):
            raise ValueError('the user can always write their own answer; remove the "Other" '
                             f'option of question {number}.')
        questions.append({'question': text, 'options': options})
    return questions


def answer_problem(questions: Sequence[Question], answers: Any) -> str | None:
    """Why `answers` does not answer `questions`, one text per question; None when it does."""
    if not isinstance(answers, list) or len(answers) != len(questions):
        return f'Answer all {len(questions)} questions.'
    for number, answer in enumerate(answers, 1):
        if not isinstance(answer, str) or not answer.strip():
            return f'Answer question {number}.'
        if len(answer.strip()) > ANSWER_CHARS:
            return f'The answer to question {number} must have at most {ANSWER_CHARS} characters.'
    return None


def answered(questions: Sequence[Question], answers: Sequence[str]) -> list[dict[str, str]]:
    return [{'question': item['question'], 'answer': answer.strip()}
            for item, answer in zip(questions, answers)]


def answers_text(questions: Sequence[Question], answers: Sequence[str]) -> str:
    """The answers as the user's message: each question, then what they answered."""
    return '\n\n'.join(f'{item["question"]}\n→ {item["answer"]}'
                       for item in answered(questions, answers))


def answers_result(questions: Sequence[Question], answers: Sequence[str]) -> str:
    """The answers as the result of the ask_user call that asked them."""
    return json.dumps({'answers': answered(questions, answers)}, ensure_ascii=False)


def questions_text(questions: Sequence[Question]) -> str:
    """Questions a stored assistant message asked, as the model reads them back."""
    return '\n'.join(f'{number}. {item["question"]} (options: {"; ".join(item["options"])})'
                     for number, item in enumerate(questions, 1))


def message_text(message: dict[str, Any]) -> str:
    """A stored conversation message as the model reads it: an assistant message with
    the questions it asked with ask_user."""
    questions = message.get('questions')
    if not questions:
        return str(message['content'])
    return '\n\n'.join(filter(None, [str(message['content']),
                                     'Asked with ask_user:\n' + questions_text(questions)]))


# Fenced and inline code may hold a `?` that asks nothing.
CODE = re.compile(r'```.*?(```|$)|`[^`\n]*`', re.DOTALL)
# A question mark that ends a sentence, not one inside a URL or an expression.
QUESTION_MARK = re.compile(r'[?？](?=[\s"\'”’)\]*_]|$)')
SENTENCE = re.compile(r'[^.!?？\n]*[?？](?=[\s"\'”’)\]*_]|$)')


def asks_directly(text: str) -> bool:
    """Whether a reply asks the user something in its own text."""
    return bool(QUESTION_MARK.search(CODE.sub('', text or '')))


def without_questions(text: str) -> str:
    """The text with each sentence that asks something removed."""
    kept = SENTENCE.sub('', text or '')
    return re.sub(r'[ \t]+\n', '\n', re.sub(r'\n{3,}', '\n\n', kept)).strip()
