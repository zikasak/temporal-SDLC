"""One-shot LLM calls on the worker ask the Claude CLI for a 5-minute prompt cache.

Seams: app.agent_workflows.host (_work_message), app.delivery.pull_request_activities
(write_pull_request_text), app.delivery.mention_activities (classify, answer,
classify_mention, answer_mention), app.tasks.activities (name_task_flow) and
app.verification.checks (analyze_checks_failure).

Each call is run with the saved settings faked to the adapter 'claude' and
app.llm.claude.ClaudeSDKClient replaced by a fake that records the ClaudeAgentOptions it is
given; the CLI environment in those options must hold FORCE_PROMPT_CACHING_5M=1, under an
API-key login as under a subscription login.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from temporalio.testing import ActivityEnvironment

import app.agent_workflows.host as host
import app.delivery.mention_activities as mention_activities
import app.delivery.pull_request_activities as pull_request_activities
import app.github.checkout as checkout
import app.llm.claude as llm_claude
import app.llm.model as llm_model
import app.settings.storage as settings_storage
import app.tasks.activities as task_activities
import app.verification.checks as checks
from app.settings.models import ModelSettings

CACHE_ENV = "FORCE_PROMPT_CACHING_5M"
LOGINS = ["api_key", "subscription"]


class FakeClaudeSDKClient:
    """Stands for claude_agent_sdk.ClaudeSDKClient (the collaborator that starts the Claude
    CLI): it records the options it is built with and answers every prompt with `reply`."""

    created: list[Any] = []
    prompts: list[str] = []
    reply: str = ""

    def __init__(self, options: Any = None, transport: Any = None) -> None:
        self.options = options
        FakeClaudeSDKClient.created.append(options)

    async def __aenter__(self) -> "FakeClaudeSDKClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.disconnect()
        return False

    async def connect(self, prompt: Any = None) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        FakeClaudeSDKClient.prompts.append(prompt if isinstance(prompt, str) else repr(prompt))

    async def receive_messages(self) -> AsyncIterator[Any]:
        text = FakeClaudeSDKClient.reply
        yield AssistantMessage(content=[TextBlock(text=text)], model="claude-fake")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
            session_id="fake-session", result=text,
        )

    async def receive_response(self) -> AsyncIterator[Any]:
        async for message in self.receive_messages():
            yield message


@pytest.fixture(params=LOGINS)
def claude(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> type[FakeClaudeSDKClient]:
    """The saved settings of every role name the adapter 'claude', under either login, and
    the Claude CLI client is the fake above."""
    FakeClaudeSDKClient.created = []
    FakeClaudeSDKClient.prompts = []
    FakeClaudeSDKClient.reply = ""

    async def get_workflow_settings(workflow_type: str) -> ModelSettings:
        return ModelSettings(
            adapter="claude", model="claude-sonnet-4-5", thinking_level="low", compaction_tokens=0,
        )

    for module in (settings_storage, llm_model, llm_claude, host, mention_activities,
                   pull_request_activities, task_activities, checks):
        if hasattr(module, "get_workflow_settings"):
            monkeypatch.setattr(module, "get_workflow_settings", get_workflow_settings)
    monkeypatch.setattr(llm_claude, "ClaudeSDKClient", FakeClaudeSDKClient)

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "FORCE_PROMPT_CACHING_5M"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "api_key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key-not-a-secret")
    else:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-subscription-token-not-a-secret")
    return FakeClaudeSDKClient


def assert_five_minute_cache(client: type[FakeClaudeSDKClient], what: str) -> None:
    """The call reached the Claude CLI and every CLI it started asks for the 5-minute cache."""
    assert client.created, f"{what}: the Claude CLI was never started"
    for options in client.created:
        env = dict(options.env or {})
        assert env.get(CACHE_ENV) == "1", (
            f"{what}: the Claude CLI environment lacks {CACHE_ENV}=1 (env keys: {sorted(env)})"
        )


class AwaitableText(str):
    """A text that may be used as it is or awaited, whichever way the code under test calls
    the faked app.github.checkout.delivery_diff."""

    def __await__(self) -> Any:
        async def value() -> str:
            return str(self)
        return value().__await__()


def fake_delivery_diff(*args: Any, **kwargs: Any) -> AwaitableText:
    return AwaitableText(
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-Widgts\n+Widgets\n"
    )


async def run_activity(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    return await ActivityEnvironment().run(fn, *args)


class TestCommitMessage:
    async def test_work_message_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient],
    ) -> None:
        # requirement: FR-192, FR-193, FR-195
        claude.reply = "Add the widget endpoint\n\nServe widgets over HTTP."
        await host._work_message(
            "implementation", "implementation", "completed",
            "diff --git a/app.py b/app.py\n+def widgets():\n+    return []\n",
        )
        assert_five_minute_cache(claude, "commit message")


class TestPullRequestText:
    async def test_pull_request_text_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = json.dumps({
            "title": "Fix the README spelling",
            "summary": "Corrects the spelling of widgets in the README.",
        })
        for module in (checkout, pull_request_activities):
            if hasattr(module, "delivery_diff"):
                monkeypatch.setattr(module, "delivery_diff", fake_delivery_diff)
        publication = {
            "branch": "delivery/sess-1", "commit": "0123456789abcdef0123456789abcdef01234567",
            "url": "https://github.com/acme/widgets/tree/delivery/sess-1",
            "ignored": [], "ignored_count": 0, "verified": True,
        }
        change = {
            "kind": "bugfix", "task": "Fix the spelling of widgets in the README",
            "requirements": [{"id": "R1", "title": "Spelling", "description": "Spell widgets right"}],
            "repository": {"full_name": "acme/widgets", "branch": "main"},
            "checkout": {"commit": "0123456789abcdef0123456789abcdef01234567"},
        }
        try:
            await run_activity(
                pull_request_activities.write_pull_request_text, "sess-1", publication, change,
            )
        except Exception:
            # Only the options the CLI was given are asserted; the shape of the faked reply
            # or of the change is not what this test is about.
            if not claude.created:
                raise
        assert_five_minute_cache(claude, "pull request title and description")


@pytest.fixture
def generated_code_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The generated-code directory (app.config.GENERATED_CODE_DIR, where answering a mention
    makes a working directory for the session) is a writable temporary directory. The value is
    bound when each module is imported, so every module holding it is patched."""
    import app.config as config

    directory = tmp_path / "generated"
    directory.mkdir()
    for module in (config, mention_activities, pull_request_activities):
        if hasattr(module, "GENERATED_CODE_DIR"):
            monkeypatch.setattr(module, "GENERATED_CODE_DIR", directory)
    return directory


class TestMentions:
    async def test_classify_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient],
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = "clarification"
        await mention_activities.classify("sess-1", "@flows-app why does this endpoint return 404?")
        assert_five_minute_cache(claude, "mention classification")

    async def test_classify_activity_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient],
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = "fix"
        await run_activity(
            mention_activities.classify_mention, "sess-1", "@flows-app please fix the typo",
        )
        assert_five_minute_cache(claude, "mention classification activity")

    async def test_answer_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient], generated_code_dir: Path,
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = "The endpoint returns 404 because no widget exists yet."
        await mention_activities.answer(
            "sess-1", "@flows-app why does this endpoint return 404?",
            {"task": "Serve widgets", "requirements": [], "messages": []},
        )
        assert_five_minute_cache(claude, "mention answer")

    async def test_answer_activity_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient], generated_code_dir: Path,
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = "Because no widget exists yet."
        await run_activity(
            mention_activities.answer_mention, "sess-1", "@flows-app why 404?",
            {"task": "Serve widgets", "requirements": [], "messages": []},
        )
        assert_five_minute_cache(claude, "mention answer activity")


class TestTaskFlowName:
    async def test_naming_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient],
    ) -> None:
        # requirement: FR-194, FR-195
        claude.reply = "Widget service"
        await run_activity(
            task_activities.name_task_flow, "Build a service that serves widgets", [],
        )
        assert_five_minute_cache(claude, "task flow naming")


class TestFailureDiagnosis:
    async def test_diagnosis_requests_the_five_minute_cache(
        self, claude: type[FakeClaudeSDKClient],
    ) -> None:
        # requirement: FR-194, FR-195, FR-196
        claude.reply = json.dumps({"verdict": "implementation", "summary": "GET /widgets answers 500"})
        await run_activity(
            checks.analyze_checks_failure, "sess-1",
            "FAILED test_widgets.py::test_list - assert 500 == 200",
        )
        assert_five_minute_cache(claude, "verification failure diagnosis")
