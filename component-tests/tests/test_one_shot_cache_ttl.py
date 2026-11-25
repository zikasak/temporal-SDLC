"""The one-shot LLM calls on the worker run the Claude CLI with a 5-minute prompt cache.

Seams: app.agent_workflows.host._work_message, app.delivery.pull_request_activities
.write_pull_request_text, app.delivery.mention_activities (classify, answer,
classify_mention, answer_mention), app.tasks.activities.name_task_flow and
app.verification.checks.analyze_checks_failure.

The role's saved settings (the adapter 'claude') and the Claude SDK client are the only
collaborators faked; the test reads the ClaudeAgentOptions.env the client is given.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

import pytest

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions

from app.settings.models import ModelSettings

VARIABLE = "FORCE_PROMPT_CACHING_5M"
LOGINS = ["api_key", "subscription"]


def _build(cls: type, **values: Any) -> Any:
    """Build an SDK message, passing only the fields this SDK version knows."""
    parameters = inspect.signature(cls).parameters
    kwargs = {name: value for name, value in values.items() if name in parameters}
    for name, parameter in parameters.items():
        if name not in kwargs and parameter.default is inspect.Parameter.empty \
                and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            kwargs[name] = None
    return cls(**kwargs)


class FakeClaudeSDKClient:
    """Stands in for app.llm.claude.ClaudeSDKClient: records its options, answers at once."""

    instances: list["FakeClaudeSDKClient"] = []
    reply = "ok"

    def __init__(self, options: ClaudeAgentOptions | None = None, transport: Any = None) -> None:
        self.options = options
        self.prompts: list[Any] = []
        FakeClaudeSDKClient.instances.append(self)

    async def __aenter__(self) -> "FakeClaudeSDKClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def connect(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def query(self, prompt: Any, *args: Any, **kwargs: Any) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> Any:
        block = _build(claude_agent_sdk.TextBlock, text=self.reply)
        yield _build(claude_agent_sdk.AssistantMessage, content=[block], model="claude-test")
        yield _build(
            claude_agent_sdk.ResultMessage, subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0, usage={},
            result=self.reply)

    async def receive_messages(self) -> Any:
        async for message in self.receive_response():
            yield message


@pytest.fixture(params=LOGINS)
def claude(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> type[FakeClaudeSDKClient]:
    """Saved settings name the claude adapter; the SDK client is replaced; one login each."""
    settings = ModelSettings(
        adapter="claude", model="claude-sonnet-4-5", thinking_level="medium", compaction_tokens=0)

    async def get_workflow_settings(workflow_type: str) -> ModelSettings:
        return settings

    import app.llm.model
    import app.settings.storage
    monkeypatch.setattr(app.settings.storage, "get_workflow_settings", get_workflow_settings)
    monkeypatch.setattr(app.llm.model, "get_workflow_settings", get_workflow_settings)

    import app.llm.claude
    monkeypatch.setattr(app.llm.claude, "ClaudeSDKClient", FakeClaudeSDKClient)

    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "api_key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    else:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-subscription-token")

    FakeClaudeSDKClient.instances = []
    FakeClaudeSDKClient.reply = "ok"
    return FakeClaudeSDKClient


async def _run(call: Callable[[], Awaitable[Any]]) -> str:
    """Run the one-shot call; its result does not matter here, only the CLI it started.
    A failure of the call itself shows in the assertion below as the missing client."""
    try:
        await call()
    except Exception as error:  # noqa: BLE001
        return f"{type(error).__name__}: {error}"
    return ""


def _assert_five_minute_cache(client: type[FakeClaudeSDKClient], what: str, failure: str) -> None:
    assert client.instances, f"{what} started no Claude CLI client (call failed with: {failure!r})"
    for instance in client.instances:
        assert instance.options is not None, f"{what}: the Claude client got no options"
        env = dict(instance.options.env or {})
        assert env.get(VARIABLE) == "1", (
            f"{what}: the Claude CLI environment lacks {VARIABLE}=1 "
            f"(has {sorted(env)}), so its cache writes use the 1-hour TTL")


class TestOneShotCallsUseFiveMinuteCache:
    async def test_commit_message_call(self, claude: type[FakeClaudeSDKClient]) -> None:
        # requirement: FR-192, FR-193
        from app.agent_workflows.host import _work_message

        diff = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"
        failure = await _run(lambda: _work_message("implementation", "implementation", "passed", diff))
        _assert_five_minute_cache(claude, "the commit-message call", failure)

    async def test_pull_request_text_call(
        self, claude: type[FakeClaudeSDKClient], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # requirement: FR-194
        import app.delivery.pull_request_activities as module
        import app.github.checkout as checkout

        async def token() -> tuple[str, dict[str, Any]]:
            return "test-token", {}

        async def delivery_diff(*args: Any, **kwargs: Any) -> str:
            return "diff --git a/app.py b/app.py\n+new line\n"

        def sync_delivery_diff(*args: Any, **kwargs: Any) -> str:
            return "diff --git a/app.py b/app.py\n+new line\n"

        monkeypatch.setattr(module, "_github_token", token)
        diff_fake = delivery_diff if inspect.iscoroutinefunction(
            getattr(checkout, "delivery_diff", None)) else sync_delivery_diff
        monkeypatch.setattr(checkout, "delivery_diff", diff_fake, raising=False)
        monkeypatch.setattr(module, "delivery_diff", diff_fake, raising=False)
        claude.reply = '{"title": "Add retries", "summary": "Adds retries to the client."}'

        publication = {
            "commit": "0123456789abcdef0123456789abcdef01234567", "branch": "feature/x",
            "unverified": False,
        }
        change = {"task": "Add retries", "requirements": []}
        failure = await _run(lambda: module.write_pull_request_text("session-1", publication, change))
        _assert_five_minute_cache(claude, "the pull request text call", failure)

    @pytest.mark.parametrize("name", ["classify", "classify_mention"])
    async def test_mention_classification_call(
        self, claude: type[FakeClaudeSDKClient], name: str,
    ) -> None:
        # requirement: FR-195
        import app.delivery.mention_activities as module

        function = getattr(module, name)
        failure = await _run(lambda: function("session-1", "@app please fix the typo"))
        _assert_five_minute_cache(claude, f"{name}", failure)

    @pytest.mark.parametrize("name", ["answer", "answer_mention"])
    async def test_mention_answer_call(self, claude: type[FakeClaudeSDKClient], name: str) -> None:
        # requirement: FR-195
        import app.delivery.mention_activities as module

        function = getattr(module, name)
        failure = await _run(lambda: function("session-1", "@app why was this changed?", {}))
        _assert_five_minute_cache(claude, f"{name}", failure)

    async def test_task_flow_naming_call(self, claude: type[FakeClaudeSDKClient]) -> None:
        # requirement: FR-195
        from app.tasks.activities import name_task_flow

        failure = await _run(lambda: name_task_flow("Add retries to the HTTP client", []))
        _assert_five_minute_cache(claude, "the task flow naming call", failure)

    async def test_verification_failure_diagnosis_call(self, claude: type[FakeClaudeSDKClient]) -> None:
        # requirement: FR-195, FR-196
        from app.verification.checks import analyze_checks_failure

        failure = await _run(lambda: analyze_checks_failure(
            "session-1", "FAILED test_login - AssertionError: expected 200, got 500"))
        _assert_five_minute_cache(claude, "the verification failure diagnosis call", failure)
