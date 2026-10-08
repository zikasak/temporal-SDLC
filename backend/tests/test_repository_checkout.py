"""Checkout and publication against a real Git remote on disk; only GitHub is replaced."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.github import checkout, storage
from app.temporal import activities

TOKEN = "github_pat_SECRET_SENTINEL"
SESSION = "api-design-session-" + "a" * 32
BRANCH = "generated/" + "a" * 12


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args], cwd=cwd,
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch, repository_selection):
    """A bare repository standing in for github.com, and a saved connection."""
    bare = tmp_path / "remote" / (repository_selection["full_name"] + ".git")
    bare.mkdir(parents=True)
    git("init", "--quiet", "--bare", "--initial-branch", repository_selection["branch"], cwd=bare)
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(bare), str(seed), cwd=tmp_path)
    for name, text in {"README.md": "project", "src/main/Old.java": "obsolete",
                       "src/main/Kept.java": "kept", "src/test/UnitTest.java": "unit test",
                       "contract-tests/src/test/java/StaleTest.java": "stale"}.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git("add", "--all", cwd=seed)
    git("commit", "--quiet", "-m", "Initial", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:refs/heads/" + repository_selection["branch"], cwd=seed)

    key = Fernet.generate_key()
    row = {"encrypted_token": Fernet(key).encrypt(TOKEN.encode()), "user_id": 7,
           "login": "octocat", "validated_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}

    async def get_connection():
        return row

    monkeypatch.setattr(storage, "GITHUB_TOKEN_ENCRYPTION_KEY", key.decode())
    monkeypatch.setattr(storage, "get_connection", get_connection)
    monkeypatch.setattr(checkout, "GITHUB_URL", f"file://{tmp_path / 'remote'}/")
    monkeypatch.setattr(activities, "GENERATED_CODE_DIR", tmp_path / "generated")
    return bare


async def test_verified_sources_are_pushed_to_a_new_branch(tmp_path, remote, repository_selection):
    base = git("rev-parse", repository_selection["branch"], cwd=remote)
    environment = ActivityEnvironment()
    result = await environment.run(activities.checkout_repository, SESSION, repository_selection)
    assert result == {"commit": base}
    session = tmp_path / "generated" / SESSION
    assert (session / "repository/src/main/Kept.java").read_text() == "kept"

    # A repeated checkout (an activity retry, a restarted run) keeps the working copy.
    (session / "repository/marker").write_text("local")
    assert await environment.run(activities.checkout_repository, SESSION, repository_selection) == result
    assert (session / "repository/marker").exists()
    (session / "repository/marker").unlink()

    with pytest.raises(ApplicationError, match="unavailable"):
        await environment.run(activities.publish_repository, SESSION, repository_selection)
    for name, text in {"implementation/service/src/main/Kept.java": "kept, extended",
                       "implementation/service/src/main/Orders.java": "new service",
                       "implementation/service/Dockerfile": "FROM scratch",
                       "tests/suite/src/test/java/OrdersTest.java": "contract test",
                       "tests/suite/pom.xml": "pom"}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)

    published = await environment.run(activities.publish_repository, SESSION, repository_selection)
    assert published == {"branch": BRANCH, "commit": git("rev-parse", BRANCH, cwd=remote),
                         "url": f"{repository_selection['html_url']}/tree/{BRANCH}"}
    files = set(git("ls-tree", "-r", "--name-only", BRANCH, cwd=remote).splitlines())
    # The author's tree replaces what it was shown (Old.java and README.md are gone, as
    # it removed them); the unit tests it never saw stay, and the suite replaces the
    # repository's contract tests.
    assert files == {"src/main/Kept.java", "src/main/Orders.java", "Dockerfile",
                     "src/test/UnitTest.java", "contract-tests/pom.xml",
                     "contract-tests/src/test/java/OrdersTest.java"}
    assert git("show", f"{BRANCH}:src/main/Kept.java", cwd=remote) == "kept, extended"
    assert git("rev-parse", f"{BRANCH}~1", cwd=remote) == base
    assert git("log", "-1", "--format=%an <%ae>", BRANCH, cwd=remote) == (
        "octocat <7+octocat@users.noreply.github.com>")
    # The selected branch is never written to, and the token is not left on disk.
    assert git("rev-parse", repository_selection["branch"], cwd=remote) == base
    assert TOKEN not in (session / "repository/.git/config").read_text()

    # Repeating a push that already happened succeeds without another commit.
    assert await environment.run(activities.publish_repository, SESSION, repository_selection) == published


async def test_a_diverged_remote_branch_is_reported_not_overwritten(tmp_path, remote, repository_selection):
    environment = ActivityEnvironment()
    await environment.run(activities.checkout_repository, SESSION, repository_selection)
    session = tmp_path / "generated" / SESSION
    for name in ("implementation/service/Dockerfile", "tests/suite/pom.xml"):
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text("generated")
    other = tmp_path / "other"
    git("clone", "--quiet", str(remote), str(other), cwd=tmp_path)
    (other / "theirs").write_text("someone else's work")
    git("add", "--all", cwd=other)
    git("commit", "--quiet", "-m", "Theirs", cwd=other)
    git("push", "--quiet", "origin", "HEAD:refs/heads/" + BRANCH, cwd=other)
    theirs = git("rev-parse", BRANCH, cwd=remote)

    with pytest.raises(ApplicationError, match="git push failed") as raised:
        await environment.run(activities.publish_repository, SESSION, repository_selection)
    assert TOKEN not in str(raised.value)
    assert git("rev-parse", BRANCH, cwd=remote) == theirs


async def test_missing_connection_fails_the_checkout_without_retries(remote, monkeypatch, repository_selection):
    async def get_connection():
        return None

    monkeypatch.setattr(storage, "get_connection", get_connection)
    with pytest.raises(ApplicationError, match="Connect GitHub") as raised:
        await ActivityEnvironment().run(activities.checkout_repository, SESSION, repository_selection)
    assert raised.value.non_retryable
