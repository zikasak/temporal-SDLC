"""docker() stops a cancelled or timed-out docker process gracefully and always reaps it.

Seam: app.docker_agents (docker). The real `docker` function runs; the docker CLI it starts
is a fake `docker` executable first on PATH. The fake writes its PID atomically (after it has
installed its signal handlers), records every SIGTERM it receives, and either ignores
SIGTERM or exits on it.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from pathlib import Path

import pytest

import app.docker_agents as docker_agents

GRACE_SECONDS = 10

FAKE_DOCKER = """#!{python}
import os, signal, sys, time

PID_FILE = {pid_file!r}
TERM_FILE = {term_file!r}
EXIT_ON_TERM = {exit_on_term!r}


def on_term(signum, frame):
    with open(TERM_FILE, "a") as marker:
        marker.write("TERM\\n")
    if EXIT_ON_TERM:
        os._exit(0)


signal.signal(signal.SIGTERM, on_term)
temporary = PID_FILE + ".tmp"
with open(temporary, "w") as handle:
    handle.write(str(os.getpid()))
os.replace(temporary, PID_FILE)
while True:
    time.sleep(0.05)
"""


class FakeDockerCli:
    """A fake `docker` executable first on PATH, and what a test can see of it."""

    def __init__(self, root: Path, exit_on_term: bool) -> None:
        self.pid_file = root / "docker.pid"
        self.term_file = root / "docker.term"
        bin_dir = root / "fake-bin"
        bin_dir.mkdir()
        executable = bin_dir / "docker"
        executable.write_text(FAKE_DOCKER.format(
            python=sys.executable, pid_file=str(self.pid_file),
            term_file=str(self.term_file), exit_on_term=exit_on_term))
        executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
        self.bin_dir = bin_dir

    def terms(self) -> int:
        if not self.term_file.exists():
            return 0
        return len(self.term_file.read_text().split())

    async def pid(self) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.pid_file.exists():
                return int(self.pid_file.read_text())
            await asyncio.sleep(0.05)
        pytest.fail("timed out: the fake docker did not write its PID")
        raise AssertionError("unreachable")


@pytest.fixture
def make_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def make(exit_on_term: bool) -> FakeDockerCli:
        cli = FakeDockerCli(tmp_path, exit_on_term)
        monkeypatch.setenv("PATH", f"{cli.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        return cli
    return make


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def assert_gone(pid: int, what: str) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    # do not leave the process behind when the assertion fails
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    pytest.fail(f"os.kill({pid}, 0) did not raise ProcessLookupError: {what}")


async def finished(task: "asyncio.Future[str]", timeout: float, what: str) -> None:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        pytest.fail(f"timed out: {what}")


async def start(args: tuple[str, ...] = ("build", "."), **kwargs: object) -> "asyncio.Future[str]":
    return asyncio.ensure_future(docker_agents.docker(*args, **kwargs))  # type: ignore[arg-type]


class TestGracefulTermination:
    async def test_cancel_sends_sigterm_and_a_process_that_exits_is_not_waited_out(
        self, make_cli,
    ) -> None:
        # requirement: FR-203, FR-204
        cli = make_cli(exit_on_term=True)
        task = await start()
        pid = await cli.pid()

        started = time.monotonic()
        task.cancel()
        await finished(task, 8, "docker() did not return after the cancel of a process that exits on SIGTERM")
        elapsed = time.monotonic() - started

        assert task.cancelled(), "the cancelled docker() call did not raise CancelledError"
        assert cli.terms() == 1, f"the fake docker received {cli.terms()} SIGTERM signals, expected 1"
        assert elapsed < GRACE_SECONDS - 2, (
            f"docker() waited {elapsed:.1f}s for a process that exited on SIGTERM")
        assert_gone(pid, "the process that exited on SIGTERM was not reaped")

    async def test_timeout_sends_sigterm_and_a_process_that_exits_is_not_waited_out(
        self, make_cli,
    ) -> None:
        # requirement: FR-203
        cli = make_cli(exit_on_term=True)
        task = await start(timeout=1)
        pid = await cli.pid()

        started = time.monotonic()
        await finished(task, 8, "docker() did not return after its timeout expired")
        elapsed = time.monotonic() - started

        assert not task.cancelled()
        assert task.exception() is not None, "docker() returned normally although its timeout expired"
        assert cli.terms() == 1, f"the fake docker received {cli.terms()} SIGTERM signals, expected 1"
        assert elapsed < GRACE_SECONDS - 2, (
            f"docker() waited {elapsed:.1f}s for a process that exited on SIGTERM")
        assert_gone(pid, "the timed-out process was not reaped")

    async def test_timeout_kills_a_process_ignoring_sigterm_only_after_the_grace_period(
        self, make_cli,
    ) -> None:
        # requirement: FR-203
        cli = make_cli(exit_on_term=False)
        began = time.monotonic()
        task = await start(timeout=1)
        pid = await cli.pid()

        # the timeout (1s) has expired by now: SIGTERM first, the process ignores it
        await asyncio.sleep(max(0.0, began + 3 - time.monotonic()))
        assert cli.terms() >= 1, "no SIGTERM reached the fake docker after its timeout expired"
        assert alive(pid), "the process that ignores SIGTERM was killed before the grace period ended"
        assert not task.done(), "docker() returned during the grace period of a running process"

        await finished(task, 20, "docker() did not return after the grace period of the timeout")
        elapsed = time.monotonic() - began

        assert not task.cancelled(), "the timed-out docker() call raised CancelledError"
        assert task.exception() is not None, "docker() returned normally although its timeout expired"
        assert elapsed >= GRACE_SECONDS, (
            f"the process was killed {elapsed:.1f}s after the call began, before the "
            f"{GRACE_SECONDS}s grace period that follows the 1s timeout")
        assert_gone(pid, "the process that ignored SIGTERM was not killed and reaped after the timeout")

    async def test_process_ignoring_sigterm_is_killed_after_the_grace_period_and_reaped(
        self, make_cli,
    ) -> None:
        # requirement: FR-203, FR-206
        cli = make_cli(exit_on_term=False)
        task = await start()
        pid = await cli.pid()

        started = time.monotonic()
        task.cancel()
        # SIGTERM first: the process that ignores it is still running well into the grace period
        await asyncio.sleep(2)
        assert cli.terms() >= 1, "no SIGTERM reached the fake docker after the cancel"
        assert alive(pid), "the process that ignores SIGTERM was killed before the grace period ended"
        assert not task.done(), "docker() returned during the grace period of a running process"

        await finished(task, 20, "docker() did not return after the grace period")
        elapsed = time.monotonic() - started

        assert task.cancelled(), "the cancelled docker() call did not raise CancelledError"
        assert elapsed >= GRACE_SECONDS - 1, (
            f"the process was killed after {elapsed:.1f}s, before the {GRACE_SECONDS}s grace period")
        assert_gone(pid, "the process that ignored SIGTERM was not killed and reaped")


class TestReapIsShielded:
    async def test_second_cancel_during_the_wait_leaves_no_running_process(
        self, make_cli,
    ) -> None:
        # requirement: FR-204, FR-207
        cli = make_cli(exit_on_term=False)
        task = await start()
        pid = await cli.pid()

        task.cancel()
        await asyncio.sleep(1)
        assert alive(pid), "the process that ignores SIGTERM was killed at once"
        assert not task.done(), "docker() returned while its process was still running"

        task.cancel()
        await asyncio.sleep(1)
        # the wait for the stopped process is shielded: the second cancel does not end it early
        assert not task.done(), (
            "the second cancel ended docker() while the process was still running")
        assert alive(pid), "the process was gone before the grace period ended"

        await finished(task, 20, "docker() did not return after the second cancel")

        assert task.cancelled(), "the twice-cancelled docker() call did not raise CancelledError"
        assert_gone(pid, "the process was still there when CancelledError reached the caller")
