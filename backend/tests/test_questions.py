import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from app.api.errors import request_validation_response
from app.api.routes import requirements as requirements_routes
from app.llm.questions import ANSWER_CHARS, answer_problem, asks_directly, without_questions
from app.temporal.interview import MAX_MESSAGE_CHARS, MAX_TRANSCRIPT_BYTES, check_answers, check_message


@pytest.mark.parametrize("text, asks", [
    ("Which database should I use?", True),
    ("I chose Postgres. Should I add Redis too? It helps caching.", True),
    ("Is it «fast»?)", True),
    ("Call GET /rooms?status=free to list free rooms.", False),
    ("Run `curl /rooms?page=2` and read ```\nif x?\n``` the result.", False),
    ("Everything is recorded.", False),
])
def test_a_question_mark_asks_only_where_it_ends_a_sentence(text, asks):
    assert asks_directly(text) is asks


def test_questions_are_dropped_from_a_reply_that_comes_with_ask_user():
    assert without_questions("I recorded FR-1. Which rooms?\n\nSee /rooms?all=1 below.") == (
        "I recorded FR-1.\n\nSee /rooms?all=1 below.")


def test_answer_problem_accepts_large_answers_up_to_limit():
    questions = [{"question": "Describe the specification.", "options": ["Option A", "Option B"]}]
    assert ANSWER_CHARS >= 50000
    assert answer_problem(questions, ["a" * ANSWER_CHARS]) is None
    assert answer_problem(questions, ["a" * (ANSWER_CHARS + 1)]) is not None


def test_check_answers_allows_large_answers_beyond_message_limit():
    questions = [
        {"question": "First question?", "options": ["A", "B"]},
        {"question": "Second question?", "options": ["C", "D"]},
    ]
    messages = [{"role": "assistant", "content": "Please answer.", "questions": questions}]
    big = "x" * 40000
    assert len(big) > MAX_MESSAGE_CHARS
    # Two 40k answers exceed both MAX_MESSAGE_CHARS (10k) and the old 64 KiB cap.
    check_answers(messages, [big, big], "too long")

    with pytest.raises(ValueError, match="1 to"):
        check_message(messages, "x" * (MAX_MESSAGE_CHARS + 1), "too long")

    oversized = [{"role": "user", "content": "y" * MAX_TRANSCRIPT_BYTES}, *messages]
    with pytest.raises(ValueError, match="too long"):
        check_answers(oversized, ["A", "C"], "too long")


async def test_answer_routes_accept_large_answer_and_reject_oversized(monkeypatch):
    received = []

    async def fake_answer_questions(session_id: str, answers: list[str]) -> None:
        received.append(("interview", session_id, answers))

    async def fake_delivery_answer(session_id: str, workflow_id: str, answers: list[str]) -> None:
        received.append(("delivery", session_id, workflow_id, answers))

    monkeypatch.setattr(requirements_routes, "answer_questions", fake_answer_questions)
    monkeypatch.setattr(requirements_routes, "answer_delivery_questions", fake_delivery_answer)

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_response)
    app.include_router(requirements_routes.router)
    session_id = "requirements-session-" + "0" * 32
    large_answer = "z" * ANSWER_CHARS

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok_interview = await client.post(
            f"/requirements/sessions/{session_id}/answers",
            json={"answers": [large_answer]},
        )
        assert ok_interview.status_code == 202, ok_interview.text

        ok_delivery = await client.post(
            f"/requirements/sessions/{session_id}/delivery/answers",
            json={"workflow_id": "stage-1", "answers": [large_answer]},
        )
        assert ok_delivery.status_code == 202, ok_delivery.text

        too_big = "z" * (ANSWER_CHARS + 1)
        bad_interview = await client.post(
            f"/requirements/sessions/{session_id}/answers",
            json={"answers": [too_big]},
        )
        assert bad_interview.status_code == 422

        bad_delivery = await client.post(
            f"/requirements/sessions/{session_id}/delivery/answers",
            json={"workflow_id": "stage-1", "answers": [too_big]},
        )
        assert bad_delivery.status_code == 422

    assert received == [
        ("interview", session_id, [large_answer]),
        ("delivery", session_id, "stage-1", [large_answer]),
    ]
