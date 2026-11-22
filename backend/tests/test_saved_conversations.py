"""Which saved provider conversation a turn continues, through the real activities."""
import dataclasses
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from temporalio.testing import ActivityEnvironment

from app.temporal import activities


class Model:
    """Stands in for the provider: each turn calls a tool, then replies."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return AIMessage(content="", tool_calls=[
                {"name": "look_around", "args": {}, "id": f"call-{self.reply}"}])
        return AIMessage(content=self.reply)


def use_models(monkeypatch, *models):
    queue = iter(models)

    async def get_model(workflow_type):
        return next(queue)
    monkeypatch.setattr(activities, "get_model", get_model)


def tool_calls_seen(request):
    return {message.tool_call_id for message in request if isinstance(message, ToolMessage)}


def texts(request):
    return " ".join(message.text for message in request)


def environment(workflow_id):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id=workflow_id)
    return env


async def test_branches_of_one_chat_request_continue_only_their_parent(
        monkeypatch, conversation_storage):
    async def nothing(*args):
        return []
    monkeypatch.setattr(activities, "list_generated_documents", nothing)
    monkeypatch.setattr(activities, "list_session_passages", nothing)
    parent, first, second, later = (Model("Parent reply"), Model("First reply"),
                                    Model("Second reply"), Model("Later reply"))
    use_models(monkeypatch, parent, first, second, later)
    session = "chat-session"
    opening = [{"role": "user", "content": "Hello"}]
    answered = [*opening, {"role": "assistant", "content": "Parent reply"}]

    await environment("request-parent").run(activities.send_llm_message, opening, session)
    # The same completed request continued twice, in one chat session.
    await environment("request-first").run(
        activities.send_llm_message, [*answered, {"role": "user", "content": "First branch"}],
        session, None, "request-parent")
    await environment("request-second").run(
        activities.send_llm_message, [*answered, {"role": "user", "content": "Second branch"}],
        session, None, "request-parent")
    await environment("request-later").run(
        activities.send_llm_message,
        [*answered, {"role": "user", "content": "Second branch"},
         {"role": "assistant", "content": "Second reply"}, {"role": "user", "content": "Go on"}],
        session, None, "request-second")

    assert tool_calls_seen(second.requests[0]) == {"call-Parent reply"}
    assert "First branch" not in texts(second.requests[0])
    assert tool_calls_seen(later.requests[0]) == {"call-Parent reply", "call-Second reply"}
    assert "First branch" not in texts(later.requests[0])


async def test_a_refreshed_checkout_starts_the_provider_conversation_anew(
        monkeypatch, conversation_storage, tmp_path):
    monkeypatch.setattr(activities, "GENERATED_CODE_DIR", tmp_path)
    session = tmp_path / "test"
    (session / "repository").mkdir(parents=True)

    def checked_out(commit):
        (session / "repository.json").write_text(json.dumps(
            {"full_name": "acme/rooms", "branch": "main", "commit": commit}))

    first, second, third = Model("Noted rooms."), Model("Noted desks."), Model("Noted all.")
    use_models(monkeypatch, first, second, third)
    opening = [{"role": "assistant", "content": "Hi"},
               {"role": "user", "content": "A new service"}]
    replied = [*opening, {"role": "assistant", "content": "Noted rooms."}]
    checked_out("a" * 40)
    await ActivityEnvironment().run(activities.requirements_turn, opening, [])
    await ActivityEnvironment().run(
        activities.requirements_turn,
        [*replied, {"role": "user", "content": "Meeting rooms"}], [])
    checked_out("b" * 40)
    await ActivityEnvironment().run(
        activities.requirements_turn,
        [*replied, {"role": "user", "content": "Meeting rooms"},
         {"role": "assistant", "content": "Noted desks."}, {"role": "user", "content": "No desks"}], [])

    # The same checkout continues the earlier tool results; another one does not.
    assert tool_calls_seen(second.requests[0]) == {"call-Noted rooms."}
    assert tool_calls_seen(third.requests[0]) == set()
    assert isinstance(third.requests[0][-1], HumanMessage)
