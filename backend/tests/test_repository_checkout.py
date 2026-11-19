"""Checkout and publication against a real Git remote on disk; only GitHub is replaced."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.delivery.contract import (
    ContractError, RepositoryContract, read_contract, write_contract,
)
from app.github import checkout, storage
from app.temporal import activities

TOKEN = "ghs_SECRET_SENTINEL"
TOKEN_PATH = "/app/installations/99/access_tokens"
SESSION = "api-design-session-" + "a" * 32
BRANCH = "generated/" + "a" * 12
CONTRACT: RepositoryContract = {
    "language": "java",
    "requirements": [{"id": "FR-1", "category": "functional", "title": "Place an order",
                      "description": "A customer places an order.", "priority": "must"}],
    "requirements_document": "# Orders\n",
    "api": {
        "info": {"title": "Orders", "version": "1.0.0", "description": "", "auth": "none"},
        "schemas": [],
        "endpoints": [{
            "method": "POST", "path": "/orders", "summary": "Place an order", "description": "",
            "tag": "orders", "parameters": [], "request_schema": None, "response_status": 201,
            "response_schema": None, "response_array": False, "error_statuses": [],
            "requirement_ids": ["FR-1"],
        }],
    },
    "architecture": "# Orders architecture\n",
    "architecture_structure": {
        "components": [{"name": "Orders API", "responsibilities": ["Place orders"]}],
        "data_flows": [], "technology_choices": []},
}


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args], cwd=cwd,
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch, repository_selection, github_connection):
    """A bare repository standing in for github.com, and a saved connection."""
    bare = tmp_path / "remote" / (repository_selection["full_name"] + ".git")
    bare.mkdir(parents=True)
    git("init", "--quiet", "--bare", "--initial-branch", repository_selection["branch"], cwd=bare)
    seed = tmp_path / "seed"
    git("clone", "--quiet", str(bare), str(seed), cwd=tmp_path)
    for name, text in {"README.md": "project", ".gitignore": "build/\n", "src/main/Old.java": "obsolete",
                       "src/main/Kept.java": "kept", "src/test/UnitTest.java": "unit test",
                       "contract-tests/src/test/java/StaleTest.java": "stale",
                       "contracts/obsolete.json": "previous contract"}.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git("add", "--all", cwd=seed)
    git("commit", "--quiet", "-m", "Initial", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:refs/heads/" + repository_selection["branch"], cwd=seed)

    github_connection(TOKEN)
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
        await environment.run(activities.publish_repository, SESSION, repository_selection,
                              CONTRACT)
    for name, text in {"implementation/service/src/main/Kept.java": "kept, extended",
                       "implementation/service/src/main/Orders.java": "new service",
                       "implementation/service/Dockerfile": "FROM scratch",
                       "implementation/service/.gitignore": "build/\n",
                       "implementation/service/build/classes/Orders.class": "compiled",
                       "implementation/service/contracts/api.json": "the author's own",
                       "tests/suite/src/test/java/OrdersTest.java": "contract test",
                       "tests/suite/pom.xml": "pom"}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    (session / "implementation/service/mvnw").write_text("#!/bin/sh")
    (session / "implementation/service/mvnw").chmod(0o755)

    published = await environment.run(activities.publish_repository, SESSION, repository_selection,
                              CONTRACT)
    assert published == {"branch": BRANCH, "commit": git("rev-parse", BRANCH, cwd=remote),
                         "url": f"{repository_selection['html_url']}/tree/{BRANCH}",
                         "ignored": ["build/"], "ignored_count": 1, "verified": True,
                         "findings": [], "findings_count": 0}
    files = set(git("ls-tree", "-r", "--name-only", BRANCH, cwd=remote).splitlines())
    # The author's tree replaces what it was shown (Old.java and README.md are gone, as
    # it removed them); the unit tests it never saw stay, and the suite replaces the
    # repository's contract tests. Build output the repository ignores stays out of
    # the branch and is reported instead. The contract is the task's, whatever the
    # repository or the author had in its place.
    assert git("ls-tree", BRANCH, "mvnw", cwd=remote).startswith("100755 ")
    assert files == {"src/main/Kept.java", "src/main/Orders.java", "Dockerfile", "mvnw", ".gitignore",
                     "src/test/UnitTest.java", "contract-tests/pom.xml",
                     "contract-tests/src/test/java/OrdersTest.java", "contracts/language.json",
                     "contracts/requirements.json", "contracts/requirements.md",
                     "contracts/api.json", "contracts/openapi.yaml",
                     "contracts/architecture.md", "contracts/architecture.json"}
    assert "/orders" in git("show", f"{BRANCH}:contracts/openapi.yaml", cwd=remote)
    # A later task reads back exactly what this one was built from.
    assert read_contract(session / "repository") == CONTRACT
    assert git("show", f"{BRANCH}:src/main/Kept.java", cwd=remote) == "kept, extended"
    assert git("rev-parse", f"{BRANCH}~1", cwd=remote) == base
    assert git("log", "-1", "--format=%an <%ae>", BRANCH, cwd=remote) == (
        "my-app[bot] <77+my-app[bot]@users.noreply.github.com>")
    # The selected branch is never written to, and the token is not left on disk.
    assert git("rev-parse", repository_selection["branch"], cwd=remote) == base
    assert TOKEN not in (session / "repository/.git/config").read_text()

    # Repeating a push that already happened succeeds without another commit.
    assert await environment.run(activities.publish_repository, SESSION, repository_selection,
                              CONTRACT) == published


async def test_bugfix_of_a_requirements_session_gets_its_own_branch(tmp_path, remote, repository_selection):
    session_id = "requirements-session-" + "b" * 32
    environment = ActivityEnvironment()
    await environment.run(activities.checkout_repository, session_id, repository_selection)
    session = tmp_path / "generated" / session_id
    # The seeded repository was never published to by this pipeline.
    with pytest.raises(ApplicationError, match="no readable contract") as refused:
        await environment.run(activities.read_repository_contract, session_id)
    assert refused.value.type == "ContractError" and refused.value.non_retryable
    for name in ("implementation/service/src/main/Kept.java", "tests/suite/pom.xml"):
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text("fixed")
    published = await environment.run(activities.publish_repository, session_id,
                                      repository_selection, CONTRACT, "bugfix")
    # Named after this session, not after the kind of session it is.
    assert published["branch"] == "generated/" + "b" * 12
    assert git("log", "-1", "--format=%s", published["branch"], cwd=remote) == (
        "Fix the service and add regression tests")
    # The next task on that branch starts from the contract published with it.
    assert await environment.run(activities.read_repository_contract, session_id) == CONTRACT


async def test_a_change_with_component_tests_pushes_them_with_its_component_contract(
        tmp_path, remote, repository_selection):
    environment = ActivityEnvironment()
    await environment.run(activities.checkout_repository, SESSION, repository_selection)
    session = tmp_path / "generated" / SESSION
    working_copy = session / "repository"
    # The repository's own component tests and stubs, as a delivery before this one left
    # them, and a file beside the suite that it does not publish.
    for name, text in {"component-tests/tests/test_old.py": "old component test",
                       "component-tests/notes.txt": "kept beside the suite",
                       "contracts/components/app/old.pyi": "published stub"}.items():
        (working_copy / name).parent.mkdir(parents=True, exist_ok=True)
        (working_copy / name).write_text(text)
    for name, text in {"implementation/service/app/rooms.py": "fixed",
                       "component-tests/suite/tests/test_rooms.py": "component test",
                       "component-tests/suite/pytest.ini": "[pytest]",
                       "component-tests/suite/requirements.txt": "pytest",
                       "routing/components/runtime.json": "{}",
                       "routing/components/app/old.pyi": "published stub",
                       "routing/components/app/rooms.pyi": "new stub"}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    contract: RepositoryContract = {**CONTRACT, "language": "python"}

    published = await environment.run(activities.publish_repository, SESSION,
                                      repository_selection, contract, "bugfix", None,
                                      {"contract_tests": False})
    files = set(git("ls-tree", "-r", "--name-only", published["branch"], cwd=remote).splitlines())
    # The suite replaces the published files of the repository's; the rest stay.
    assert {"component-tests/tests/test_rooms.py", "component-tests/pytest.ini",
            "component-tests/requirements.txt", "component-tests/notes.txt"} <= files
    assert "component-tests/tests/test_old.py" not in files
    # The checked component contract is published beside the rest of the contract.
    assert {"contracts/components/runtime.json", "contracts/components/app/old.pyi",
            "contracts/components/app/rooms.pyi", "contracts/openapi.yaml"} <= files
    # No author of the change wrote contract tests: the repository's stay as they are.
    assert git("show", f"{published['branch']}:contract-tests/src/test/java/StaleTest.java",
               cwd=remote) == "stale"
    message = git("log", "-1", "--format=%B", published["branch"], cwd=remote)
    assert "component tests in component-tests/" in message

    # A later push of a change without component tests keeps the component contract.
    for name in ("implementation/service/app/rooms.py", "tests/suite/tests/test_api.py"):
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text("again")
    await environment.run(activities.publish_repository, SESSION, repository_selection,
                          contract, "bugfix")
    files = set(git("ls-tree", "-r", "--name-only", published["branch"], cwd=remote).splitlines())
    assert {"contracts/components/app/rooms.pyi", "component-tests/tests/test_rooms.py",
            "contract-tests/tests/test_api.py"} <= files

    # Without the component tests there is nothing to push.
    for name in ("component-tests/suite", "routing/components"):
        subprocess.run(["rm", "-r", str(session / name)], check=True)
    with pytest.raises(ApplicationError, match="unavailable"):
        await environment.run(activities.publish_repository, SESSION, repository_selection,
                              contract, "bugfix", None, {"contract_tests": False})


async def test_a_change_with_component_tests_of_its_web_client_pushes_them_in_the_part(
        tmp_path, remote, repository_selection):
    environment = ActivityEnvironment()
    await environment.run(activities.checkout_repository, SESSION, repository_selection)
    session = tmp_path / "generated" / SESSION
    working_copy = session / "repository"
    for name, text in {"frontend/component-tests/tests/old.test.ts": "old part test",
                       "frontend/component-tests/notes.txt": "kept beside the suite",
                       "component-tests/tests/test_old.py": "the service's own"}.items():
        (working_copy / name).parent.mkdir(parents=True, exist_ok=True)
        (working_copy / name).write_text(text)
    for name, text in {
            "implementation/service/app/rooms.py": "fixed",
            "implementation/service/frontend/src/github.ts": "fixed client",
            "component-tests/suite/parts/frontend/tests/manifest.test.ts": "part test",
            "component-tests/suite/parts/frontend/package.json": "{}",
            "component-tests/suite/parts/frontend/package-lock.json": "{}",
            "routing/routes.json": json.dumps({"routes": {}, "parts": [
                {"name": "frontend", "root": "frontend", "language": "typescript"}]}),
            "routing/parts/frontend/components/runtime.json": '{"project": "frontend"}',
            "routing/parts/frontend/components/src/github.d.ts": "stub"}.items():
        (session / name).parent.mkdir(parents=True, exist_ok=True)
        (session / name).write_text(text)
    contract: RepositoryContract = {**CONTRACT, "language": "python"}

    published = await environment.run(activities.publish_repository, SESSION,
                                      repository_selection, contract, "bugfix", None,
                                      {"contract_tests": False})
    files = set(git("ls-tree", "-r", "--name-only", published["branch"], cwd=remote).splitlines())
    # The part's suite replaces what it publishes of the part's component tests.
    assert {"frontend/component-tests/tests/manifest.test.ts",
            "frontend/component-tests/package.json", "frontend/component-tests/notes.txt",
            "frontend/src/github.ts",
            "contracts/parts/frontend/components/runtime.json",
            "contracts/parts/frontend/components/src/github.d.ts"} <= files
    assert "frontend/component-tests/tests/old.test.ts" not in files
    # The service had no criterion of its own: its component tests stay as they were.
    assert "component-tests/tests/test_old.py" in files
    assert not any(name.startswith(("component-tests/parts", "contracts/components"))
                   for name in files)
    message = git("log", "-1", "--format=%B", published["branch"], cwd=remote)
    assert "P/component-tests/" in message


def test_writing_the_contract_keeps_the_component_contract(tmp_path):
    for name in ("contracts/obsolete.json", "contracts/old/nested.json",
                 "contracts/components/app/rooms.pyi"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("before")
    write_contract(tmp_path, CONTRACT)
    assert read_contract(tmp_path) == CONTRACT
    assert not (tmp_path / "contracts/obsolete.json").exists()
    assert not (tmp_path / "contracts/old").exists()
    assert (tmp_path / "contracts/components/app/rooms.pyi").read_text() == "before"


async def test_documentation_is_pushed_alone_and_marked_as_unverified(tmp_path, remote, repository_selection):
    environment = ActivityEnvironment()
    await environment.run(activities.checkout_repository, SESSION, repository_selection)
    session = tmp_path / "generated" / SESSION
    before = set(git("ls-tree", "-r", "--name-only", repository_selection["branch"],
                     cwd=remote).splitlines())
    for name, text in {"README.md": "rewritten", "docs/guide.md": "added",
                       "src/main/Kept.java": "not documentation"}.items():
        (session / "documentation/files" / name).parent.mkdir(parents=True, exist_ok=True)
        (session / "documentation/files" / name).write_text(text)

    published = await environment.run(activities.publish_documentation, SESSION,
                                      repository_selection, "java", False)
    assert published["branch"] == BRANCH and published["verified"] is False
    # The commit itself says that nothing checked the change.
    message = git("log", "-1", "--format=%B", BRANCH, cwd=remote)
    assert message.startswith("Change the documentation (unverified)\n")
    assert "Not verified" in message
    # Only documentation changes: the service, its tests and the contract stay as
    # the repository held them.
    assert set(git("ls-tree", "-r", "--name-only", BRANCH, cwd=remote).splitlines()) == (
        before | {"docs/guide.md"})
    assert git("show", f"{BRANCH}:README.md", cwd=remote) == "rewritten"
    assert git("show", f"{BRANCH}:src/main/Kept.java", cwd=remote) == "kept"
    assert git("show", f"{BRANCH}:contracts/obsolete.json", cwd=remote) == "previous contract"


@pytest.mark.parametrize("damage", [
    None,
    {"requirements.json": '{"FR-1": "Place an order"}'},
    {"requirements.json": '["Place an order"]'},
    {"requirements.json": '[{"id": 1, "category": "functional"}]'},
    {"requirements.json": '[{"id": "FR-1", "category": "wishes", "title": "T", '
                          '"description": "D", "priority": "must"}]'},
    {"language.json": '{"name": ["java"]}'},
    {"api.json": '{"info": {}, "endpoints": "none"}'},
])
def test_a_repository_without_a_usable_contract_is_reported_as_unsupported(tmp_path, damage):
    """Whatever is wrong with the files, the session is told, not left to fail later."""
    if damage:
        write_contract(tmp_path, CONTRACT)
        for name, text in damage.items():
            (tmp_path / "contracts" / name).write_text(text)
    with pytest.raises(ContractError, match="no readable contract in contracts/"):
        read_contract(tmp_path)


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
        await environment.run(activities.publish_repository, SESSION, repository_selection,
                              CONTRACT)
    assert TOKEN not in str(raised.value)
    assert git("rev-parse", BRANCH, cwd=remote) == theirs


async def test_a_session_checks_its_branch_out_again_at_its_latest_commit(tmp_path, remote, repository_selection):
    environment = ActivityEnvironment()
    before = await environment.run(activities.checkout_repository, SESSION, repository_selection)
    working_copy = tmp_path / "generated" / SESSION / "repository"
    (working_copy / "marker").write_text("local")
    seed = tmp_path / "seed"
    write_contract(seed, CONTRACT)
    git("add", "--all", cwd=seed)
    git("commit", "--quiet", "-m", "Publish the contract", cwd=seed)
    git("push", "--quiet", "origin", "HEAD:refs/heads/" + repository_selection["branch"], cwd=seed)

    # A plain checkout keeps the copy the session already has.
    assert await environment.run(activities.checkout_repository, SESSION,
                                 repository_selection) == before
    with pytest.raises(ContractError):
        read_contract(working_copy)

    after = await environment.run(activities.update_repository_checkout, SESSION,
                                  repository_selection)
    assert after == {"commit": git("rev-parse", "HEAD", cwd=seed)} != before
    assert read_contract(working_copy) == CONTRACT
    assert not (working_copy / "marker").exists()
    # It is what a later checkout of the session finds.
    assert await environment.run(activities.checkout_repository, SESSION,
                                 repository_selection) == after


async def test_missing_connection_fails_the_checkout_without_retries(remote, monkeypatch, repository_selection):
    async def get_connection():
        return None

    monkeypatch.setattr(storage, "get_connection", get_connection)
    with pytest.raises(ApplicationError, match="Connect GitHub") as raised:
        await ActivityEnvironment().run(activities.checkout_repository, SESSION, repository_selection)
    assert raised.value.non_retryable


@pytest.mark.parametrize("upstream,message,final", [
    (httpx.Response(404), "no longer installed", True),
    (httpx.Response(401), "rejected the app credentials", True),
    (httpx.ConnectError("unreachable"), "Could not reach GitHub", False),
])
async def test_only_a_token_failure_an_operator_must_fix_stops_the_retries(
        remote, github_http, repository_selection, upstream, message, final):
    github_http[0][TOKEN_PATH] = upstream
    with pytest.raises(ApplicationError, match=message) as raised:
        await ActivityEnvironment().run(activities.checkout_repository, SESSION, repository_selection)
    assert raised.value.non_retryable is final


async def test_chat_reads_the_checked_out_repository(remote, monkeypatch, repository_selection):
    class Model:
        def __init__(self):
            self.replies = iter([AIMessage(content="Done")])

        def bind_tools(self, tools):
            self.tools = {item.name for item in tools}
            return self

        async def ainvoke(self, messages, config=None):
            self.messages = list(messages)
            return next(self.replies)

    async def nothing(*args):
        return []

    model = Model()
    async def get_model(workflow_type):
        assert workflow_type == 'chat'
        return model
    monkeypatch.setattr(activities, "get_model", get_model)
    monkeypatch.setattr(activities, "list_generated_documents", nothing)
    monkeypatch.setattr(activities, "list_session_passages", nothing)
    session = "llm-request-" + "b" * 32
    question = [{"role": "user", "content": "Tell me about the project"}]
    environment = ActivityEnvironment()

    # Without a working copy the model is told so instead of being handed file tools.
    assert await environment.run(
        activities.send_llm_message, question, session, repository_selection) == "Done"
    assert not model.tools & {"list_files", "search_files", "read_file"}
    assert "could not be checked out" in model.messages[0].content

    await environment.run(activities.checkout_repository, session, repository_selection)
    model.replies = iter([
        AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"path": "README.md"}, "id": "readme"}]),
        AIMessage(content="It is a project"),
    ])
    assert await environment.run(
        activities.send_llm_message, question, session, repository_selection) == "It is a project"
    assert {"list_files", "search_files", "read_file"} <= model.tools
    assert not model.tools & {"write_file", "run_command"}
    assert repository_selection["full_name"] in model.messages[0].content
    assert "project" in model.messages[-1].content
    # A conversation started without a repository is not given another session's files.
    model.replies = iter([AIMessage(content="No repository")])
    await environment.run(activities.send_llm_message, question, session)
    assert not model.tools & {"list_files", "search_files", "read_file"}
