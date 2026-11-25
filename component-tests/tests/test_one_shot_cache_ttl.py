"""The one-shot worker calls made through the Claude adapter request a 5-minute prompt cache.

Each call obtains its model with `get_model(<workflow type>)`; the tests fake that name in the
module that uses it to return a real `ChatClaude`, and fake `claude_agent_sdk.ClaudeSDKClient`
to record the `ClaudeAgentOptions` it is given. Only the environment of those options (the
environment of the Claude CLI) is asserted.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Awaitable, Callable

import claude_agent_sdk
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import app.llm.claude as claude_module
from app.llm.claude import ChatClaude

CACHE_FLAG = "FORCE_PROMPT_CACHING_5M"

# The Claude logins: an API key, or a subscription (OAuth token). Dummy values only.
LOGINS = {
    "api_key": {"ANTHROPIC_API_KEY": "dummy-api-key"},
    "subscription": {"CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-token"},
}


class FakeClaudeClient:
    """Stands for claude_agent_sdk.ClaudeSDKClient: records the options it was built with and
    answers at once with a fixed reply."""

    created: list[Any] = []
    reply: str = "ok"

    def __init__(self, options: Any = None, transport: Any = None) -> None:
        self.options = options
        FakeClaudeClient.created.append(options)

    async def __aenter__(self) -> "FakeClaudeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def set_model(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def set_permission_mode(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_server_info(self) -> dict[str, Any]:
        return {}

    async def query(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def _messages(self) -> Any:
        yield AssistantMessage(
            content=[TextBlock(text=FakeClaudeClient.reply)], model="claude-test")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="fake-session", result=FakeClaudeClient.reply)

    def receive_response(self) -> Any:
        return self._messages()

    def receive_messages(self) -> Any:
        return self._messages()


def options_env(options: Any) -> dict[str, str]:
    env = options.env if not isinstance(options, dict) else options.get("env")
    return dict(env or {})


@pytest.fixture
def claude_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """The options of every Claude CLI call started during the test."""
    FakeClaudeClient.created = []
    FakeClaudeClient.reply = "ok"
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClaudeClient)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClaudeClient, raising=False)
    return FakeClaudeClient.created


def use_claude_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str,
) -> list[str]:
    """Fake get_model, in the module that uses it, to return a ChatClaude; returns the
    workflow types it was asked for."""
    asked: list[str] = []

    async def get_model(name: str, *args: Any, **kwargs: Any) -> ChatClaude:
        asked.append(name)
        return ChatClaude(model="claude-test", cli_path="claude", config_dir=tmp_path)

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "get_model", get_model, raising=False)
    model_module = importlib.import_module("app.llm.model")
    monkeypatch.setattr(model_module, "get_model", get_model, raising=False)
    return asked


def login_as(monkeypatch: pytest.MonkeyPatch, login: str) -> None:
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in LOGINS[login].items():
        monkeypatch.setenv(name, value)


async def run_call(call: Callable[[], Awaitable[Any]]) -> None:
    """Run the call under test. What it returns is not the subject here, only the environment
    of the Claude CLI it started, so a failure to read the fake's reply is not one."""
    try:
        await call()
    except Exception:
        pass


def assert_five_minute_cache(calls: list[Any], what: str) -> None:
    assert calls, f"{what} did not start the Claude CLI"
    for options in calls:
        env = options_env(options)
        assert env.get(CACHE_FLAG) == "1", (
            f"{what}: the Claude CLI environment lacks {CACHE_FLAG}=1 (has "
            f"{ {k: v for k, v in env.items() if 'KEY' not in k and 'TOKEN' not in k} })")
        assert env.get("CLAUDE_CODE_ENTRYPOINT") == "temporal-sdlc", (
            f"{what}: the other CLI environment variables must stay unchanged")


async def call_work_message() -> Any:
    from app.agent_workflows.host import _work_message
    FakeClaudeClient.reply = "Add the widget endpoint"
    return await _work_message(
        "implementation", "implementation", "completed",
        "diff --git a/app.py b/app.py\n+def widgets():\n+    return []\n")


async def call_pull_request_text() -> Any:
    from app.delivery.pull_request_activities import write_pull_request_text
    FakeClaudeClient.reply = '{"title": "Add widgets", "summary": "Adds the widget endpoint."}'
    publication = {
        "branch": "delivery/sess-1", "commit": "0123456789abcdef0123456789abcdef01234567",
        "url": "https://github.com/acme/widgets/tree/delivery/sess-1",
        "verified": True, "ignored": [], "ignored_count": 0,
    }
    change = {
        "task": "Serve widgets", "kind": "api_change", "diff": "+def widgets(): ...\n",
        "requirements": [{"id": "R1", "title": "Widgets", "description": "Serve widgets"}],
        "requirements_document": "# Requirements\n\nServe widgets.\n",
        "open_findings": [], "findings": [], "verified": True,
    }
    return await write_pull_request_text("sess-1", publication, change)


async def call_classify() -> Any:
    from app.delivery.mention_activities import classify
    FakeClaudeClient.reply = "fix"
    return await classify("sess-1", "@flows-app please fix the typo in the README")


async def call_answer() -> Any:
    from app.delivery.mention_activities import answer
    FakeClaudeClient.reply = "The widget list is served from /widgets."
    return await answer(
        "sess-1", "@flows-app why does /widgets return a list?",
        {"requirements": [{"id": "R1", "title": "Widgets"}], "diff": "+def widgets(): ..."})


async def call_name_task_flow() -> Any:
    from app.tasks.activities import name_task_flow
    FakeClaudeClient.reply = "Widget service"
    return await name_task_flow(
        "Serve widgets over HTTP", [{"id": "R1", "title": "Serve widgets"}])


async def call_analyze_failure() -> Any:
    from app.verification.checks import analyze_checks_failure
    FakeClaudeClient.reply = '{"verdict": "implementation", "summary": "The endpoint 500s."}'
    return await analyze_checks_failure(
        "sess-1", "FAILED test_widgets.py::test_list - assert 500 == 200\n")


ONE_SHOT_CALLS = {
    "pull_request_text": ("app.delivery.pull_request_activities", call_pull_request_text),
    "mention_classification": ("app.delivery.mention_activities", call_classify),
    "mention_answer": ("app.delivery.mention_activities", call_answer),
    "task_flow_naming": ("app.tasks.activities", call_name_task_flow),
    "failure_diagnosis": ("app.verification.checks", call_analyze_failure),
}


class TestCommitMessageCache:
    @pytest.mark.parametrize("login", sorted(LOGINS))
    async def test_commit_message_call_requests_five_minute_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude_calls: list[Any],
        login: str,
    ) -> None:
        # requirement: FR-185, FR-186, FR-188, FR-189
        login_as(monkeypatch, login)
        use_claude_model(monkeypatch, tmp_path, "app.agent_workflows.host")

        await run_call(call_work_message)

        assert_five_minute_cache(claude_calls, f"the commit-message call ({login} login)")


class TestOtherOneShotCallsCache:
    @pytest.mark.parametrize("login", sorted(LOGINS))
    @pytest.mark.parametrize("call", sorted(ONE_SHOT_CALLS))
    async def test_one_shot_call_requests_five_minute_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude_calls: list[Any],
        call: str, login: str,
    ) -> None:
        # requirement: FR-187, FR-188, FR-189
        module_name, invoke = ONE_SHOT_CALLS[call]
        login_as(monkeypatch, login)
        use_claude_model(monkeypatch, tmp_path, module_name)

        await run_call(invoke)

        assert_five_minute_cache(claude_calls, f"the {call} call ({login} login)")
