"""A Chat answer heartbeats after each model request and tells the model once to finish
when the activity's deadline is near, through the real activity."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage
from temporalio.testing import ActivityEnvironment

from app.temporal import activities

NOTICE = "The time for this request is nearly up"


class Model:
    """Stands in for the provider: calls a tool twice, then replies."""

    def __init__(self):
        self.requests = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.requests.append(list(messages))
        if len(self.requests) < 3:
            return AIMessage(content="", tool_calls=[
                {"name": "look_around", "args": {}, "id": f"call-{len(self.requests)}"}])
        return AIMessage(content="Found it")


@pytest.fixture
def model(monkeypatch):
    async def nothing(*args):
        return []
    monkeypatch.setattr(activities, "list_generated_documents", nothing)
    monkeypatch.setattr(activities, "list_session_passages", nothing)
    provider = Model()

    async def get_model(workflow_type):
        return provider
    monkeypatch.setattr(activities, "get_model", get_model)
    return provider


def environment(running_for: timedelta, heartbeats: list):
    env = ActivityEnvironment()
    now = datetime.now(timezone.utc)
    env.info = dataclasses.replace(
        env.info, workflow_id="request-deadline",
        scheduled_time=now - running_for, current_attempt_scheduled_time=now - running_for,
        started_time=now - running_for,
        start_to_close_timeout=timedelta(minutes=100),
        schedule_to_close_timeout=timedelta(minutes=101),
        heartbeat_timeout=timedelta(minutes=6))
    env.on_heartbeat = lambda *details: heartbeats.append(details)
    return env


def notices(request):
    return sum(NOTICE in message.text for message in request)


async def test_model_is_told_once_to_finish_when_the_deadline_is_near(model):
    heartbeats = []
    answer = await environment(timedelta(minutes=90), heartbeats).run(
        activities.send_llm_message, "try to find a bug", "chat-session")

    assert answer == "Found it"
    assert [notices(request) for request in model.requests] == [0, 1, 1]
    assert len(heartbeats) == 3


async def test_model_is_not_told_to_finish_while_time_remains(model):
    heartbeats = []
    await environment(timedelta(minutes=10), heartbeats).run(
        activities.send_llm_message, "try to find a bug", "chat-session")

    assert [notices(request) for request in model.requests] == [0, 0, 0]
    assert len(heartbeats) == 3
