"""The one-shot LLM calls of the worker run the Claude CLI with a 5-minute prompt cache.

The calls go through the native Claude adapter. A test makes the saved settings of the role
name the adapter 'claude', replaces the Claude SDK client (the process the service would
start) with a fake that records the ClaudeAgentOptions it is given, runs the call and reads
the environment the CLI would get: it must hold FORCE_PROMPT_CACHING_5M=1.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from temporalio.testing import ActivityEnvironment

from app.settings.models import ModelSettings

SETTING = "FORCE_PROMPT_CACHING_5M"


class FakeClaudeClient:
    """Stands for claude_agent_sdk.ClaudeSDKClient: starts no CLI, records the options it is
    given and answers every query with `reply`."""

    instances: list["FakeClaudeClient"] = []
    reply: str = ""

    def __init__(self, options: Any = None, *args: Any, **kwargs: Any) -> None:
        self.options = options
        self.prompts: list[Any] = []
        FakeClaudeClient.instances.append(self)

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

    async def query(self, prompt: Any = "", *args: Any, **kwargs: Any) -> None:
        self.prompts.append(prompt)

    async def receive_messages(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=self.reply)], model="claude-sonnet-4-5")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
            session_id="s", result=self.reply,
        )

    def receive_response(self) -> AsyncIterator[Any]:
        return self.receive_messages()


def app_modules_with(name: str) -> list[Any]:
    return [m for key, m in list(sys.modules.items())
            if key.startswith("app.") and m is not None and hasattr(m, name)]


@pytest.fixture(params=["api_key", "subscription"])
def claude(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> type[FakeClaudeClient]:
    """Saved settings that choose the claude adapter, the fake client, and a Claude login
    by API key or by subscription."""
    import importlib

    import app.llm.claude as claude_module
    # Import every module of the seams first, so each name the settings are imported by
    # is replaced below.
    for name in ("app.llm.model", "app.settings.storage", "app.agent_workflows.host",
                 "app.delivery.pull_request_activities", "app.delivery.mention_activities",
                 "app.tasks.activities", "app.verification.checks"):
        importlib.import_module(name)

    async def get_workflow_settings(workflow_type: str) -> ModelSettings:
        return ModelSettings(
            adapter="claude", model="claude-sonnet-4-5", thinking_level="medium",
            compaction_tokens=48000,
        )

    async def get_openai_endpoint() -> None:
        return None

    for module in app_modules_with("get_workflow_settings"):
        monkeypatch.setattr(module, "get_workflow_settings", get_workflow_settings)
    for module in app_modules_with("get_openai_endpoint"):
        monkeypatch.setattr(module, "get_openai_endpoint", get_openai_endpoint)
    monkeypatch.setattr(claude_module, "ClaudeSDKClient", FakeClaudeClient)
    # The calls create a working directory per session under GENERATED_CODE_DIR: keep it in
    # a temporary directory so the call reaches the client and leaves nothing behind.
    generated = tmp_path / "generated"
    generated.mkdir()
    for module in app_modules_with("GENERATED_CODE_DIR"):
        monkeypatch.setattr(module, "GENERATED_CODE_DIR", generated)

    for variable in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", SETTING):
        monkeypatch.delenv(variable, raising=False)
    if request.param == "api_key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key-not-a-secret")
    else:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-subscription-token-not-a-secret")

    FakeClaudeClient.instances = []
    FakeClaudeClient.reply = ""
    CALL_ERRORS.clear()
    return FakeClaudeClient


CALL_ERRORS: list[BaseException] = []


async def run_call(call: Callable[[], Awaitable[Any]]) -> None:
    """Run the one-shot call. What it returns is not what a test observes (an unusable reply
    makes these calls fall back), so a failure it raises on the fake's reply is not either:
    the options its CLI was started with are. The error is kept to be reported when no CLI
    was started."""
    try:
        await call()
    except Exception as error:  # noqa: BLE001
        CALL_ERRORS.append(error)


def assert_five_minute_cache(claude: type[FakeClaudeClient], what: str) -> None:
    assert claude.instances, (
        f"{what} started no Claude CLI through the claude adapter"
        + (f" (the call raised {CALL_ERRORS[-1]!r})" if CALL_ERRORS else "")
    )
    for client in claude.instances:
        options = client.options
        assert options is not None, f"{what} started the Claude CLI without options"
        env = dict(options.env or {})
        assert env.get(SETTING) == "1", (
            f"{what}: the Claude CLI environment has {SETTING}={env.get(SETTING)!r}, expected '1'"
        )


class TestCommitMessageCache:
    async def test_commit_message_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-192, FR-193
        from app.agent_workflows.host import _work_message

        claude.reply = "Add the widget endpoint"
        await run_call(lambda: _work_message(
            "implementation", "implementation", "completed",
            "diff --git a/app.py b/app.py\n+def widgets():\n+    return []\n"))
        assert_five_minute_cache(claude, "the commit-message call")


class TestPullRequestTextCache:
    async def test_pull_request_text_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # requirement: FR-194, FR-195
        from app.delivery.pull_request_activities import write_pull_request_text

        class Diff(str):
            """A diff that works whether the code awaits it or uses it as it is."""

            def __await__(self) -> Any:
                async def value() -> str:
                    return str(self)
                return value().__await__()

        diff = Diff("diff --git a/app.py b/app.py\n+def widgets():\n+    return []\n")

        def delivery_diff(*args: Any, **kwargs: Any) -> Diff:
            return diff

        import app.github.checkout  # noqa: F401
        for module in app_modules_with("delivery_diff"):
            monkeypatch.setattr(module, "delivery_diff", delivery_diff)

        claude.reply = '{"title": "Add the widget endpoint", "summary": "Adds GET /widgets."}'
        publication = {
            "branch": "delivery/sess-1", "commit": "0123456789abcdef0123456789abcdef01234567",
            "url": "https://github.com/acme/widgets/tree/delivery/sess-1",
            "ignored": [], "ignored_count": 0, "verified": True,
        }
        change = {
            "kind": "api_change", "task": "Serve widgets",
            "requirements": [{"id": "R1", "title": "Widgets", "description": "Serve widgets"}],
        }
        await run_call(lambda: ActivityEnvironment().run(
            write_pull_request_text, "sess-1", publication, change))
        assert_five_minute_cache(claude, "the pull request text call")


class TestMentionCache:
    async def test_classify_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-194, FR-195
        from app.delivery.mention_activities import classify

        claude.reply = "fix"
        await run_call(lambda: classify("sess-1", "@flows-app please fix the typo in the README"))
        assert_five_minute_cache(claude, "the mention classification call")

    async def test_classify_activity_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-194, FR-195
        from app.delivery.mention_activities import classify_mention

        claude.reply = "clarification"
        await run_call(lambda: ActivityEnvironment().run(
            classify_mention, "sess-1", "@flows-app why does the endpoint return a list?"))
        assert_five_minute_cache(claude, "the classify-mention activity")

    async def test_answer_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-194, FR-195
        from app.delivery.mention_activities import answer

        claude.reply = "The endpoint returns a list because the requirement says so."
        await run_call(lambda: answer(
            "sess-1", "@flows-app why does the endpoint return a list?", {}))
        assert_five_minute_cache(claude, "the mention answer call")

    async def test_answer_activity_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-194, FR-195
        from app.delivery.mention_activities import answer_mention

        claude.reply = "The endpoint returns a list because the requirement says so."
        await run_call(lambda: ActivityEnvironment().run(
            answer_mention, "sess-1", "@flows-app why does the endpoint return a list?", {}))
        assert_five_minute_cache(claude, "the answer-mention activity")


class TestTaskFlowNamingCache:
    async def test_task_flow_naming_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-194, FR-195
        from app.tasks.activities import name_task_flow

        claude.reply = "Widget service"
        await run_call(lambda: ActivityEnvironment().run(
            name_task_flow, "Build a service that serves widgets", []))
        assert_five_minute_cache(claude, "the task flow naming call")


class TestVerificationDiagnosisCache:
    async def test_failure_diagnosis_call_forces_five_minute_cache(
        self, claude: type[FakeClaudeClient],
    ) -> None:
        # requirement: FR-195, FR-196
        from app.verification.checks import analyze_checks_failure

        claude.reply = '{"side": "implementation", "summary": "GET /widgets returns 500"}'
        await run_call(lambda: ActivityEnvironment().run(
            analyze_checks_failure, "sess-1",
            "FAILED test_widgets.py::test_list - assert 500 == 200\n"))
        assert_five_minute_cache(claude, "the verification failure diagnosis call")
