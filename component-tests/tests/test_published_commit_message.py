"""The commit pushed when work is published has a message an LLM wrote from its diff.

Seams: app.temporal.activities (publish_repository_from, publish_onboarding_from,
publish_documentation_from, _publish and the fixed-subject constants) and
app.agent_workflows.host._work_message for the wip/ format.

Faked collaborators: the saved workflow settings (adapter 'claude'), the Claude SDK client
(app.llm.claude.ClaudeSDKClient), _github_token and the push (app.github.checkout.push), which
records the message it is given; the test reads the message of that push.
"""
from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from dataclasses import dataclass
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

from helpers import REPOSITORY, patch_everywhere

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
        # the connection fields the commit's author is built from
        return "test-token", {"app_slug": "flows", "bot_user_id": 1}

    import app.temporal.activities as activities
    monkeypatch.setattr(activities, "_github_token", fake_token)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0"})


@dataclass
class Push:
    """One call of the faked app.github.checkout.push."""
    target: Path
    branch: str
    message: str
    commit: str
    verified: bool


PUSHES: list[Push] = []


@pytest.fixture(autouse=True)
def push(monkeypatch: pytest.MonkeyPatch) -> list[Push]:
    """The push collaborator (app.github.checkout.push) is faked: the runner has no git remote.
    It records the message it is given and answers a publication."""
    PUSHES.clear()

    async def fake_push(
        target: Path, repository: Any, token: str, *, branch: str, message: str,
        author: tuple[str, str], verified: bool = True, resolved: str | None = None,
    ) -> RepositoryPublication:
        commit = f"{len(PUSHES) + 1:040x}"
        PUSHES.append(Push(Path(target), branch, message, commit, verified))
        return cast(RepositoryPublication, {
            "branch": branch, "commit": commit,
            "url": f"https://github.com/acme/widgets/tree/{branch}",
            "ignored": [], "ignored_count": 0, "verified": verified,
        })

    import app.github.checkout
    import app.temporal.activities
    patch_everywhere(monkeypatch, [app.github.checkout, app.temporal.activities], "push", fake_push)
    return PUSHES


class Remote:
    """The session's working copy: a workspace whose checkout holds the files a publication
    changes. Its pushes are recorded by the faked push; the checkout is also made a git
    repository when git is available, for code that reads the diff with it."""

    def __init__(self, tmp_path: Path, root: Path | None = None) -> None:
        self.workspace = SessionWorkspace(root=root or tmp_path / "workspace")
        checkout = self.workspace.checkout
        checkout.mkdir(parents=True, exist_ok=True)
        (checkout / "README.md").write_text("# Widgets\n")
        if shutil.which("git"):
            _git("init", "-q", "-b", "main", cwd=checkout)
            for key, value in (("user.name", "Flows"), ("user.email", "flows@example.com")):
                _git("config", key, value, cwd=checkout)
            _git("add", "-A", cwd=checkout)
            _git("commit", "-q", "-m", "Initial commit", cwd=checkout)
        for directory in (self.workspace.service, self.workspace.suite,
                          self.workspace.component_tests, self.workspace.documentation):
            directory.mkdir(parents=True, exist_ok=True)
        (self.workspace.service / "main.py").write_text("print('widgets')\n")
        (self.workspace.suite / "test_widgets.py").write_text("def test_widgets():\n    pass\n")

    def message(self, publication: RepositoryPublication) -> str:
        """The full message the push of this publication was given."""
        [pushed] = [p for p in PUSHES if p.commit == publication["commit"]]
        return pushed.message.strip()


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
        "api": {
            "info": {"title": "Widgets", "version": "1.0.0", "description": "", "auth": "none"},
            "schemas": [],
            "endpoints": [],
        },
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

        diff = f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b {MARKER}\n"
        # the stage and status are template parameters: ask with those a publication uses
        wip = await _work_message("implementation", "publication", "verified", diff)
        assert wip, "the wip/ message call returned no message"
        wip_prompt = "\n".join(llm.instances[-1].prompts)
        assert diff.strip() in wip_prompt, "the wip/ prompt does not hold the diff it was given"
        # the instructions around the diff, which the published commit's prompt must share
        before, _, after = wip_prompt.partition(diff.strip())
        assert (before + after).strip(), "the wip/ prompt holds no instructions besides the diff"

        def template(text: str) -> str:
            """Mask the stage and status words, which are parameters of the shared template."""
            return re.sub(r"\S+ stage \([^)]*\)", "<stage> (<status>)", text)

        before, after = template(before), template(after)

        def update() -> None:
            (remote.workspace.checkout / "notes.md").write_text(f"notes {MARKER}\n")

        asked_before = len(llm.instances)
        publication = await activities._publish(
            remote.workspace, SESSION, REPOSITORY, "Given fixed subject", update)
        message = remote.message(publication)

        assert len(llm.instances) > asked_before, \
            "the published commit's message was not written by the LLM"
        published_prompt = "\n".join(llm.instances[-1].prompts)
        assert MARKER in published_prompt, "the published prompt does not hold the commit's diff"
        published_prompt = template(published_prompt)
        assert published_prompt.startswith(before) and published_prompt.endswith(after), (
            "published commits are not written from the same prompt as wip/ commits: "
            f"wip/ instructions {before!r} ... {after!r}, published prompt {published_prompt!r}")
        # the same format: the LLM's subject, an empty line, then its body
        assert message.startswith(LLM_REPLY), (
            "the LLM text of the published commit is not in the wip/ format: "
            f"the LLM wrote {LLM_REPLY!r}, the commit has {message!r}")
        body = message[len(LLM_SUBJECT):].lstrip("\n").split("\n\n")[0].splitlines()
        assert len(body) <= 5


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

    async def test_bugfix_subject_longer_than_72_characters_is_bounded(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-205
        import app.temporal.activities as activities

        long_subject = ("Retry failed widget fetches with exponential backoff and jitter "
                        "so that a flaky upstream no longer fails every request of a client")
        assert len(long_subject) > 100
        llm.reply = long_subject + "\n\n- Add a retry loop to the widget client"
        publication = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="bugfix")
        subject = _subject(remote.message(publication))

        assert llm.instances, "the LLM was never asked to write the commit message"
        assert len(subject) <= 72, f"the subject has {len(subject)} characters: {subject!r}"
        assert subject != FIXED_BUGFIX_SUBJECT
        assert subject.startswith("Retry failed widget fetches")

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


class TestOtherPartsOfTheMessageAreKept:
    async def test_delivery_with_components_keeps_the_components_note(
        self, llm: type[FakeClaudeSDKClient], remote: Remote, tmp_path: Path,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        plain = await activities.publish_repository_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), kind="api_change")
        plain_message = remote.message(plain)

        other = Remote(tmp_path / "with-components")
        # the routed component contract of the service part, read from workspace.root
        (other.workspace.root / "routing" / "components").mkdir(parents=True, exist_ok=True)
        other.workspace.component_tests.mkdir(parents=True, exist_ok=True)
        (other.workspace.component_tests / "test_widgets_component.py").write_text(
            "def test_component():\n    pass\n")
        components = {"files": ["test_widgets_component.py"], "tests": 1, "passed": True}
        with_components = await activities.publish_repository_from(
            other.workspace, SESSION, REPOSITORY, _contract(), kind="api_change",
            components=components)
        message = other.message(with_components)

        assert _subject(plain_message) == LLM_SUBJECT
        assert _subject(message) == LLM_SUBJECT
        for line in LLM_BODY:
            assert line in message
        assert re_search(GENERATED, message)
        assert len(message) > len(plain_message) and message != plain_message, (
            "the component tests note is missing from the message of a delivery with components")

    async def test_onboarding_with_changed_code_keeps_the_code_changed_note(
        self, llm: type[FakeClaudeSDKClient], remote: Remote, tmp_path: Path,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        unchanged = await activities.publish_onboarding_from(
            remote.workspace, SESSION, REPOSITORY, _contract())
        unchanged_message = remote.message(unchanged)

        other = Remote(tmp_path / "code-changed")
        changed = await activities.publish_onboarding_from(
            other.workspace, SESSION, REPOSITORY, _contract(), code_changed=True)
        message = other.message(changed)

        assert _subject(unchanged_message) == LLM_SUBJECT
        assert _subject(message) == LLM_SUBJECT
        assert re_search(GENERATED, message)
        assert len(message) > len(unchanged_message) and message != unchanged_message, (
            "the code-changed note is missing from the message of an onboarding that changed code")

    async def test_onboarding_with_open_findings_keeps_marker_and_findings(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-204, FR-201
        import app.temporal.activities as activities

        findings = [_finding()]
        publication = await activities.publish_onboarding_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), findings=findings)
        message = remote.message(publication)

        assert _subject(message) == LLM_SUBJECT + " (unverified)"
        assert findings[0]["summary"] in message
        assert commit_findings(findings).strip() in message
        assert activities.FINDINGS_NOTE.strip() in message
        assert re_search(GENERATED, message)

    async def test_fallback_onboarding_keeps_marker_and_findings_on_the_fixed_subject(
        self, llm: type[FakeClaudeSDKClient], remote: Remote,
    ) -> None:
        # requirement: FR-202, FR-204
        import app.temporal.activities as activities

        llm.error = RuntimeError("the LLM is unavailable")
        findings = [_finding()]
        publication = await activities.publish_onboarding_from(
            remote.workspace, SESSION, REPOSITORY, _contract(), findings=findings)
        message = remote.message(publication)

        assert llm.instances, "the LLM was never asked before the fixed subject was used"
        assert _subject(message) == activities.ONBOARDING_SUBJECT + " (unverified)"
        assert findings[0]["summary"] in message


ARCHITECTURE = f"# Architecture\n\nOne module, core. {MARKER}\n"
STRUCTURE: dict[str, Any] = {"components": [{"name": "core"}]}


@pytest.fixture
def architecture_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Remote:
    """The session's checkout where the architecture-only publication looks for it:
    GENERATED_CODE_DIR/<session>/repository."""
    import app.temporal.activities as activities
    generated = tmp_path / "generated"
    monkeypatch.setattr(activities, "GENERATED_CODE_DIR", generated)
    return Remote(tmp_path, root=generated / SESSION)


class TestArchitectureOnlyOnboardingCommit:
    async def test_architecture_subject_is_the_llm_subject(
        self, llm: type[FakeClaudeSDKClient], architecture_remote: Remote,
    ) -> None:
        # requirement: FR-201
        import app.temporal.activities as activities

        publication = await activities.publish_onboarding_architecture(
            SESSION, REPOSITORY, ARCHITECTURE, STRUCTURE)
        message = architecture_remote.message(publication)

        assert _subject(message) == LLM_SUBJECT
        assert activities.ARCHITECTURE_SUBJECT not in message.splitlines()[0]
        for line in LLM_BODY:
            assert line in message
        assert re_search(GENERATED, message)
        _assert_llm_asked_with_diff(llm, "the architecture-only onboarding")

    @pytest.mark.parametrize("failure", ["raises", "empty"])
    async def test_architecture_push_succeeds_with_the_fixed_subject_when_the_llm_fails(
        self, llm: type[FakeClaudeSDKClient], architecture_remote: Remote, failure: str,
    ) -> None:
        # requirement: FR-202
        import app.temporal.activities as activities

        if failure == "raises":
            llm.error = RuntimeError("the LLM is unavailable")
        else:
            llm.reply = ""
        publication = await activities.publish_onboarding_architecture(
            SESSION, REPOSITORY, ARCHITECTURE, STRUCTURE)

        assert llm.instances, "the LLM was never asked before the fixed subject was used"
        assert publication["commit"]
        assert _subject(architecture_remote.message(publication)) == activities.ARCHITECTURE_SUBJECT


def re_search(pattern: str, text: str) -> bool:
    import re
    return re.search(pattern, text) is not None
