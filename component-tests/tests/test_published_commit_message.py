"""The commit pushed when work is published has a message an LLM wrote from its diff.

Seams: app.temporal.activities (publish_repository_from, publish_onboarding_from,
publish_documentation_from, _publish and the fixed-subject constants) and
app.agent_workflows.host._work_message for the wip/ format.

Faked collaborators: the saved workflow settings (adapter 'claude'), the Claude SDK client
(app.llm.claude.ClaudeSDKClient) and _github_token. The push is app.github.checkout.push as it
is, against a local bare repository that stands for origin; the test reads the pushed commit.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions

from app.delivery.contract import RepositoryContract
from app.delivery.workspace import SessionWorkspace
from app.github.models import RepositoryPublication
from app.review.models import ReviewFinding, commit_findings
from app.settings.models import ModelSettings

from helpers import REPOSITORY

SESSION = "sess-commit-1"
MARKER = "UNIQUE-DIFF-MARKER-7f3a9c"
LLM_SUBJECT = "Retry failed widget fetches with backoff"
LLM_BODY = ["- Add a retry loop to the widget client", "- Double the delay after each failure"]
LLM_REPLY = LLM_SUBJECT + "\n\n" + "\n".join(LLM_BODY)
FIXED_BUGFIX_SUBJECT = "Fix the service and add regression tests"
GENERATED = f"Generated from .+ by session {SESSION}\\."


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
    """Stands in for app.llm.claude.ClaudeSDKClient: records what it is asked, answers at once
    with `reply`, or raises `error` when set."""

    instances: list["FakeClaudeSDKClient"] = []
    reply = LLM_REPLY
    error: Exception | None = None

    def __init__(self, options: ClaudeAgentOptions | None = None, transport: Any = None) -> None:
        self.options = options
        self.prompts: list[str] = []
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
        self.prompts.append(prompt if isinstance(prompt, str) else repr(prompt))
        if FakeClaudeSDKClient.error is not None:
            raise FakeClaudeSDKClient.error

    async def receive_response(self) -> Any:
        if FakeClaudeSDKClient.error is not None:
            raise FakeClaudeSDKClient.error
        block = _build(claude_agent_sdk.TextBlock, text=self.reply)
        yield _build(claude_agent_sdk.AssistantMessage, content=[block], model="claude-test")
        yield _build(
            claude_agent_sdk.ResultMessage, subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0, usage={},
            result=self.reply)

    async def receive_messages(self) -> Any:
        async for message in self.receive_response():
            yield message

    @property
    def system_prompt(self) -> str:
        value = getattr(self.options, "system_prompt", None)
        return "" if value is None else str(value)

    @property
    def everything_asked(self) -> str:
        return self.system_prompt + "\n" + "\n".join(self.prompts)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> type[FakeClaudeSDKClient]:
    """Saved settings name the claude adapter; the SDK client is replaced."""
    settings = ModelSettings(
        adapter="claude", model="claude-sonnet-4-5", thinking_level="medium", compaction_tokens=0)

    async def get_workflow_settings(workflow_type: str) -> ModelSettings:
        return settings

    import app.llm.claude
    import app.llm.model
    import app.settings.storage
    monkeypatch.setattr(app.settings.storage, "get_workflow_settings", get_workflow_settings)
    monkeypatch.setattr(app.llm.model, "get_workflow_settings", get_workflow_settings)
    monkeypatch.setattr(app.llm.claude, "ClaudeSDKClient", FakeClaudeSDKClient)
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")

    FakeClaudeSDKClient.instances = []
    FakeClaudeSDKClient.reply = LLM_REPLY
    FakeClaudeSDKClient.error = None
    return FakeClaudeSDKClient


@pytest.fixture(autouse=True)
def github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_token() -> tuple[str, dict[str, Any]]:
        return "test-token", {}

    import app.temporal.activities as activities
    monkeypatch.setattr(activities, "_github_token", fake_token)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd or "/tmp"),
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0"})
    return result.stdout


class Remote:
    """A local bare repository standing for origin, with a workspace whose checkout is its clone."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "origin.git"
        _git("init", "--bare", "-b", "main", str(self.path))
        seed = tmp_path / "seed"
        _git("clone", str(self.path), str(seed))
        for key, value in (("user.name", "Seed"), ("user.email", "seed@example.com")):
            _git("config", key, value, cwd=seed)
        _git("checkout", "-B", "main", cwd=seed)
        (seed / "README.md").write_text("# Widgets\n")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "Initial commit", cwd=seed)
        _git("push", "origin", "main", cwd=seed)

        self.workspace = SessionWorkspace(root=tmp_path / "workspace")
        self.workspace.root.mkdir(parents=True, exist_ok=True)
        checkout = self.workspace.checkout
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", str(self.path), str(checkout))
        for key, value in (("user.name", "Flows"), ("user.email", "flows@example.com")):
            _git("config", key, value, cwd=checkout)
        for directory in (self.workspace.service, self.workspace.suite,
                          self.workspace.component_tests, self.workspace.documentation):
            directory.mkdir(parents=True, exist_ok=True)
        (self.workspace.service / "main.py").write_text("print('widgets')\n")
        (self.workspace.suite / "test_widgets.py").write_text("def test_widgets():\n    pass\n")

    def message(self, publication: RepositoryPublication) -> str:
        """The full message of the commit the push left on its branch of origin."""
        return _git("--git-dir", str(self.path), "log", "-1", "--format=%B",
                    publication["branch"]).strip()


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    return Remote(tmp_path)


def _contract() -> RepositoryContract:
    return cast(RepositoryContract, {
        "language": "python",
        "requirements": [{
            "id": "R1", "category": "functional", "priority": "must",
            "title": "Serve widgets", "description": "The service serves widgets.",
        }],
        "requirements_document": f"# Requirements\n\nServe widgets. {MARKER}\n",
        "api": {"paths": {"/widgets": {}}},
    })


def _finding() -> ReviewFinding:
    return cast(ReviewFinding, {
        "summary": "Widget deletion is not checked for ownership",
        "target": "implementation", "reason": "Any user can delete any widget.",
    })


def _subject(message: str) -> str:
    return message.splitlines()[0]


def _assert_llm_asked_with_diff(llm: type[FakeClaudeSDKClient], what: str) -> None:
    assert llm.instances, f"{what}: the LLM was never asked to write the commit message"
    asked = "\n".join(instance.everything_asked for instance in llm.instances)
    assert MARKER in asked, f"{what}: the prompt sent to the LLM does not hold the commit's diff"


async def _publish_documentation(remote: Remote, verified: bool = True) -> RepositoryPublication:
    import app.temporal.activities as activities
    (remote.workspace.documentation / "README.md").write_text(f"# Widgets\n\nDocumented. {MARKER}\n")
    return await activities.publish_documentation_from(
        remote.workspace, SESSION, REPOSITORY, "python", verified)


class TestPublishedCommitMessageIsWrittenByTheLLM:
    @pytest.mark.parametrize("kind", [None, "api_change"])
    async def test_delivery_subject_is_the_llm_subject(
        self, llm: type[FakeClaudeSDKClient], remote: Remote, kind: str | None,
    ) -> None:
        # requirement: FR-201
        import app.temporal.activities as activities

        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind=kind)
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT
        for line in LLM_BODY:
            assert line in message
        fixed = activities.COMMIT_SUBJECTS.get(kind or "", activities.COMMIT_SUBJECT)
        assert fixed not in message.splitlines()[0]
        _assert_llm_asked_with_diff(llm, "the delivery")

    async def test_onboarding_subject_is_the_llm_subject(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-201
        import app.temporal.activities as activities

        publication = await activities.publish_onboarding_from(
            remote.workspace, SESSION, REPOSITORY, _contract())
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT
        for line in LLM_BODY:
            assert line in message
        _assert_llm_asked_with_diff(llm, "the onboarding")

    async def test_documentation_subject_is_the_llm_subject(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-201
        publication = await _publish_documentation(remote)
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT
        for line in LLM_BODY:
            assert line in message
        _assert_llm_asked_with_diff(llm, "the documentation change")


class TestFallbackToTheFixedMessage:
    @pytest.fixture(params=["raises", "empty"])
    def failing(self, request: pytest.FixtureRequest, llm: type[FakeClaudeSDKClient]) -> type[FakeClaudeSDKClient]:
        if request.param == "raises":
            llm.error = RuntimeError("the LLM is unavailable")
        else:
            llm.reply = ""
        return llm

    async def test_delivery_keeps_the_fixed_subject(
        self, failing: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202
        import app.temporal.activities as activities

        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="api_change")
        message = remote.message(publication)

        assert failing.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == activities.COMMIT_SUBJECTS["api_change"]
        assert re_search(GENERATED, message)

    async def test_onboarding_keeps_the_fixed_subject(
        self, failing: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202
        import app.temporal.activities as activities

        publication = await activities.publish_onboarding_from(
            remote.workspace, SESSION, REPOSITORY, _contract())
        message = remote.message(publication)

        assert failing.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == activities.ONBOARDING_SUBJECT

    async def test_documentation_keeps_the_fixed_subject(
        self, failing: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202
        import app.temporal.activities as activities

        publication = await _publish_documentation(remote)
        message = remote.message(publication)

        assert failing.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == activities.DOCUMENTATION_SUBJECT

    async def test_publish_falls_back_to_the_subject_it_is_given(
        self, failing: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202
        import app.temporal.activities as activities

        def update() -> None:
            (remote.workspace.checkout / "notes.md").write_text(f"notes {MARKER}\n")

        publication = await activities._publish(
            remote.workspace, SESSION, REPOSITORY, "Given fixed subject", update)
        message = remote.message(publication)

        assert failing.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == "Given fixed subject"


class TestWipFormatIsShared:
    async def test_published_message_reads_like_a_wip_message(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-203
        import app.temporal.activities as activities
        from app.agent_workflows.host import _work_message

        # a reply longer than the wip/ format allows: the same reply goes through both paths
        llm.reply = LLM_SUBJECT + "\n\n" + "\n".join(f"- change number {n}" for n in range(1, 9))
        diff = f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b {MARKER}\n"
        wip = await _work_message("implementation", "implementation", "passed", diff)
        assert wip, "the wip/ message call returned no message"
        wip_prompt = llm.instances[-1]

        def update() -> None:
            (remote.workspace.checkout / "notes.md").write_text(f"notes {MARKER}\n")

        publication = await activities._publish(
            remote.workspace, SESSION, REPOSITORY, "Given fixed subject", update)
        message = remote.message(publication)

        assert len(llm.instances) > 1, "the published commit's message was not written by the LLM"
        published_prompt = llm.instances[-1]
        assert published_prompt.system_prompt == wip_prompt.system_prompt, \
            "published commits are not written from the same prompt as wip/ commits"
        assert message.startswith(wip.strip()), (
            "the LLM text of the published commit is not in the wip/ format: "
            f"wip/ gives {wip.strip()!r}, the commit has {message!r}")


class TestBugfixSubject:
    async def test_bugfix_subject_names_the_change_and_is_bounded(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-205
        import app.temporal.activities as activities

        assert activities.COMMIT_SUBJECTS["bugfix"] == FIXED_BUGFIX_SUBJECT
        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="bugfix")
        subject = _subject(remote.message(publication))

        assert subject == LLM_SUBJECT
        assert subject != FIXED_BUGFIX_SUBJECT
        assert len(subject) <= 72

    @pytest.mark.parametrize("failure", ["raises", "empty"])
    async def test_bugfix_push_succeeds_with_the_fixed_subject_when_the_llm_fails(
        self, llm: type[FakeClaudeSDKClient], remote: Remote, failure: str,
    ) -> None:
        # requirement: FR-205
        import app.temporal.activities as activities

        if failure == "raises":
            llm.error = RuntimeError("the LLM is unavailable")
        else:
            llm.reply = "   "
        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="bugfix")

        assert llm.instances, "the LLM was never asked before the fixed subject was used"
        assert publication["commit"]
        assert _subject(remote.message(publication)) == FIXED_BUGFIX_SUBJECT


class TestPublishedCommitKeepsTodaysParts:
    async def test_unverified_delivery_keeps_marker_findings_and_generated_line(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        findings = [_finding()]
        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="api_change",
            findings=findings)
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT + " (unverified)"
        assert findings[0]["summary"] in message
        assert commit_findings(findings).strip() in message
        assert activities.FINDINGS_NOTE.strip() in message
        assert re_search(GENERATED, message)
        for line in LLM_BODY:
            assert line in message

    async def test_unverified_publish_keeps_marker_details_and_notes(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        def update() -> None:
            (remote.workspace.checkout / "notes.md").write_text(f"notes {MARKER}\n")

        publication = await activities._publish(
            remote.workspace, SESSION, REPOSITORY, "Given fixed subject", update,
            verified=False, note="NOTE-OF-AN-UNVERIFIED-PUSH", details="DETAILS-OF-THE-PUSH")
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT + " (unverified)"
        assert "NOTE-OF-AN-UNVERIFIED-PUSH" in message
        assert "DETAILS-OF-THE-PUSH" in message
        assert re_search(GENERATED, message)
        assert publication["verified"] is False

    async def test_unverified_documentation_keeps_marker_and_note(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        publication = await _publish_documentation(remote, verified=False)
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT + " (unverified)"
        assert activities.UNVERIFIED_NOTE.strip() in message
        assert re_search(GENERATED, message)

    async def test_fallback_keeps_the_unverified_marker_on_the_fixed_subject(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202, FR-204
        import app.temporal.activities as activities

        llm.error = RuntimeError("the LLM is unavailable")
        publication = await _publish_documentation(remote, verified=False)
        message = remote.message(publication)

        assert llm.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == activities.DOCUMENTATION_SUBJECT + " (unverified)"
        assert activities.UNVERIFIED_NOTE.strip() in message


def re_search(pattern: str, text: str) -> bool:
    import re
    return re.search(pattern, text) is not None
