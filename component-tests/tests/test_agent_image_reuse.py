"""Agent, tool and script images are shared across sessions; containers carry the session.

Seam: app.docker_agents (AgentImageBuild, docker, run_tool, run_script). The only
collaborator faked is `docker`, the one function through which the code runs the docker CLI;
the fake records each call's args and answers by them. The `logs` call of run_script goes
through a real subprocess, which a fake `docker` executable first on PATH makes harmless.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path
from collections.abc import Coroutine
from typing import Any, Literal

import pytest

import app.docker_agents as docker_agents
from app.docker_agents import AgentImageBuild, run_script, run_tool

Kind = Literal["tool", "script"]
KINDS: list[Kind] = ["tool", "script"]


def image_prefix(role: str) -> str:
    return f"{role.replace('_', '-')}-agent:"


class FakeDocker:
    """Records every call; answers `image inspect` by the set of tags that exist locally."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.existing: set[str] = set()
        self.everything_exists = False
        self.containers: set[str] = set()

    async def __call__(
        self, *args: str, timeout: int = 1800, env: dict | None = None,
        stdin: Any = None, log_path: Path | None = None,
    ) -> str:
        self.calls.append(tuple(str(a) for a in args))
        command = args[0]
        if command == "image" and len(args) > 1 and args[1] == "inspect":
            tag = args[-1]
            if self.everything_exists or tag in self.existing:
                return "sha256:0123456789abcdef"
            raise RuntimeError(f"Error: No such image: {tag}")
        if command == "build":
            self.existing.add(args[2])
            return ""
        if command == "create":
            self.containers.add(args[args.index("--name") + 1])
            return "container-id"
        if command == "wait":
            return "0"
        if command == "cp":
            source, destination = args[1], args[2]
            if ":" in source and not source.startswith("/"):
                target = Path(destination)
                if not target.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("report\n")
            return ""
        return ""

    def named(self, command: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c and c[0] == command]

    def inspects(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[:2] == ("image", "inspect")]

    def builds(self) -> list[tuple[str, ...]]:
        return self.named("build")

    def creates(self) -> list[tuple[str, ...]]:
        return self.named("create")

    def create_tags(self, role: str) -> list[str]:
        prefix = image_prefix(role)
        tags = []
        for call in self.creates():
            found = [a for a in call if prefix in a]
            assert found, f"the 'create' call names no image of role {role!r}: {call}"
            tags.append(found[-1])
        return tags

    def create_names(self) -> list[str]:
        return [c[c.index("--name") + 1] for c in self.creates()]


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(docker_agents, "docker", fake)
    # `logs` of run_script is a real subprocess: put a harmless `docker` first on PATH.
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable = bin_dir / "docker"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake


class Inputs:
    """Build inputs over files of a temporary directory, which a test can change."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.context = root / "context"
        self.context.mkdir()
        (self.context / "agent.py").write_text("print('agent')\n")
        (self.context / "lib").mkdir()
        (self.context / "lib" / "util.py").write_text("VALUE = 1\n")
        self.dockerfile = root / "Dockerfile.agent"
        self.dockerfile.write_text("FROM python:3.12-slim\nCOPY . /opt/agent\n")
        self.build_args: dict[str, str] = {"BASE": "slim"}
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "notes.txt").write_text("workspace\n")
        self.counter = 0

    def build(self) -> AgentImageBuild:
        return AgentImageBuild(self.context, self.dockerfile, dict(self.build_args))

    async def run(self, kind: Kind, role: str, session_id: str) -> None:
        self.counter += 1
        log_path = self.root / f"run-{self.counter}.log"
        call: Coroutine[Any, Any, Any]
        if kind == "tool":
            call = run_tool(
                role, session_id, self.workspace, "echo checked", "/tmp/report.txt", log_path,
                image_build=self.build(), timeout=30)
        else:
            call = run_script(
                role, session_id, self.workspace, "echo done", log_path,
                image_build=self.build(), workdir="review", timeout=30)
        try:
            await asyncio.wait_for(call, timeout=15)
        except asyncio.TimeoutError:
            pytest.fail(f"timed out: run_{kind} of session {session_id!r} did not return")


@pytest.fixture
def inputs(tmp_path: Path) -> Inputs:
    return Inputs(tmp_path)


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def old_tags(role: str, session_id: str) -> list[str]:
    """The per-session tags of today: first 16 hex digits of sha256(session ID)."""
    short = session_hash(session_id)[:16]
    dashed = role.replace("_", "-")
    return [f"temporal-{dashed}-agent:{short}", f"{dashed}-agent:{short}"]



class TestImageTag:
    @pytest.mark.parametrize("kind", KINDS)
    async def test_sessions_with_identical_inputs_use_one_tag(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind,
    ) -> None:
        # requirement: FR-197, TR-9
        await inputs.run(kind, "implementation", "session-alpha")
        await inputs.run(kind, "implementation", "session-beta")

        tags = fake_docker.create_tags("implementation")
        assert len(tags) == 2, f"expected two containers created, got {fake_docker.creates()}"
        assert tags[0] == tags[1], f"two sessions with identical build inputs got tags {tags}"
        assert tags[0].startswith("temporal-implementation-agent:") or \
            tags[0].startswith("implementation-agent:")
        for session_id in ("session-alpha", "session-beta"):
            assert session_hash(session_id)[:16] not in tags[0], (
                f"the tag {tags[0]!r} still carries the hash of session {session_id!r}")

    async def test_tag_is_the_same_for_tool_and_script_runs(
        self, fake_docker: FakeDocker, inputs: Inputs,
    ) -> None:
        # requirement: FR-197
        await inputs.run("tool", "code_review", "session-alpha")
        await inputs.run("script", "code_review", "session-beta")

        tags = fake_docker.create_tags("code_review")
        assert tags[0] == tags[1], f"a tool run and a script run of the same inputs got {tags}"

    @pytest.mark.parametrize("change", ["dockerfile", "context_file", "new_context_file", "build_arg"])
    @pytest.mark.parametrize("kind", KINDS)
    async def test_changed_input_gives_a_different_tag(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind, change: str,
    ) -> None:
        # requirement: TR-9
        await inputs.run(kind, "implementation", "session-alpha")

        if change == "dockerfile":
            inputs.dockerfile.write_text("FROM python:3.13-slim\nCOPY . /opt/agent\n")
        elif change == "context_file":
            (inputs.context / "lib" / "util.py").write_text("VALUE = 2\n")
        elif change == "new_context_file":
            (inputs.context / "extra.py").write_text("EXTRA = True\n")
        else:
            inputs.build_args["BASE"] = "bookworm"
        await inputs.run(kind, "implementation", "session-alpha")

        tags = fake_docker.create_tags("implementation")
        assert tags[0] != tags[1], f"a changed {change} left the image tag {tags[0]!r} unchanged"


class TestReuseOfExistingImage:
    @pytest.mark.parametrize("kind", KINDS)
    async def test_existing_image_is_not_built(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind,
    ) -> None:
        # requirement: TR-11
        fake_docker.everything_exists = True

        await inputs.run(kind, "implementation", "session-alpha")

        assert fake_docker.builds() == [], f"docker build ran for an existing image: {fake_docker.builds()}"
        [create_tag] = fake_docker.create_tags("implementation")
        assert any(c[-1] == create_tag for c in fake_docker.inspects()), (
            f"the container image {create_tag!r} was not the one checked with 'image inspect'")

    @pytest.mark.parametrize("kind", KINDS)
    async def test_image_is_built_once_for_many_sessions(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind,
    ) -> None:
        # requirement: FR-197, TR-11
        for session_id in ("session-alpha", "session-beta", "session-gamma"):
            await inputs.run(kind, "implementation", session_id)

        assert len(fake_docker.creates()) == 3
        assert len(fake_docker.builds()) == 1, (
            f"three sessions with identical build inputs built the image "
            f"{len(fake_docker.builds())} times: {fake_docker.builds()}")
        # the missing image is built first, under the tag the containers are created from
        [build] = fake_docker.builds()
        tags = fake_docker.create_tags("implementation")
        assert build[1] == "-t" and build[2] == tags[0], (
            f"containers are created from {tags[0]!r} but the build was {build}")
        assert set(tags) == {tags[0]}
        assert fake_docker.calls.index(build) < fake_docker.calls.index(fake_docker.creates()[0])

    async def test_changed_inputs_build_a_new_image(
        self, fake_docker: FakeDocker, inputs: Inputs,
    ) -> None:
        # requirement: TR-9, TR-11
        await inputs.run("tool", "implementation", "session-alpha")
        inputs.build_args["BASE"] = "bookworm"
        await inputs.run("tool", "implementation", "session-alpha")

        assert len(fake_docker.builds()) == 2, f"expected a second build, got {fake_docker.builds()}"
        tags = fake_docker.create_tags("implementation")
        assert tags[0] != tags[1], f"a changed build arg left the tag {tags[0]!r} unchanged"
        assert [b[2] for b in fake_docker.builds()] == tags


class TestOldPerSessionImages:
    @pytest.mark.parametrize("kind", KINDS)
    async def test_old_image_is_not_removed_or_used(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind,
    ) -> None:
        # requirement: TR-12
        session_id = "session-alpha"
        old = old_tags("implementation", session_id)
        fake_docker.existing.update(old)

        await inputs.run(kind, "implementation", session_id)

        [create_tag] = fake_docker.create_tags("implementation")
        assert create_tag not in old, f"the container was created from the old per-session image {create_tag}"
        assert [b for b in fake_docker.builds() if b[2] in old] == []
        assert len(fake_docker.builds()) == 1, (
            "the old image does not satisfy the new tag, so the image is built under the new tag: "
            f"{fake_docker.builds()}")
        for call in fake_docker.calls:
            assert call[0] != "rmi", f"an image was removed: {call}"
            assert call[:2] not in (("image", "rm"), ("image", "prune")), f"an image was removed: {call}"
            assert call[:2] != ("system", "prune"), f"images were pruned: {call}"
        assert all(not any(tag in call for tag in old) or call[:2] == ("image", "inspect")
                   for call in fake_docker.calls), "a call other than an inspect names the old image"


class TestContainerName:
    @pytest.mark.parametrize("role", ["implementation", "code_review"])
    @pytest.mark.parametrize("kind", KINDS)
    async def test_name_has_role_kind_session_hash_and_suffix(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind, role: str,
    ) -> None:
        # requirement: TR-10
        session_id = "session-alpha"
        await inputs.run(kind, role, session_id)

        [name] = fake_docker.create_names()
        expected_prefix = f"{role.replace('_', '-')}-{kind}-{session_hash(session_id)}-"
        assert name.startswith(expected_prefix), (
            f"container name {name!r} does not start with {expected_prefix!r}")
        assert len(name) > len(expected_prefix), f"container name {name!r} has no unique suffix"

    @pytest.mark.parametrize("kind", KINDS)
    async def test_two_runs_never_share_a_name(
        self, fake_docker: FakeDocker, inputs: Inputs, kind: Kind,
    ) -> None:
        # requirement: TR-10
        await inputs.run(kind, "implementation", "session-alpha")
        await inputs.run(kind, "implementation", "session-alpha")
        await inputs.run(kind, "implementation", "session-beta")

        names = fake_docker.create_names()
        assert len(set(names)) == 3, f"container names are not unique: {names}"
        assert names[0].startswith(f"implementation-{kind}-{session_hash('session-alpha')}-")
        assert names[1].startswith(f"implementation-{kind}-{session_hash('session-alpha')}-")
        assert names[2].startswith(f"implementation-{kind}-{session_hash('session-beta')}-")
        assert names[0] != names[1]

    async def test_tool_and_script_names_keep_their_kind(
        self, fake_docker: FakeDocker, inputs: Inputs,
    ) -> None:
        # requirement: TR-10
        await inputs.run("tool", "implementation", "session-alpha")
        await inputs.run("script", "implementation", "session-alpha")

        tool_name, script_name = fake_docker.create_names()
        assert re.match(r"^implementation-tool-[0-9a-f]{64}-.+$", tool_name), tool_name
        assert re.match(r"^implementation-script-[0-9a-f]{64}-.+$", script_name), script_name
