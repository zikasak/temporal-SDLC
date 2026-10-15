import pytest

from app.llm.questions import asks_directly, without_questions


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
