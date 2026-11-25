from typing import Any
from collections.abc import Mapping
from pathlib import Path
from dataclasses import dataclass
@dataclass(frozen=True)
class AgentImageBuild:
    """Trusted build inputs of an agent image: the build context directory, the Dockerfile
    path and the build args. A test builds one over files in a temporary directory."""
    context: Path
    dockerfile: Path
    build_args: Mapping[str, str]
    def __init__(self, context: Path, dockerfile: Path, build_args: Mapping[str, str] = ...) -> None: ...
async def docker(*args: str, timeout: int=1800, env: dict | None=None, stdin: Any=None, log_path: Path | None=None) -> str:
    """The one function through which the code runs the docker CLI; a test replaces it
    (monkeypatch app.docker_agents.docker) with a fake that records each call's args and
    answers by them, returning the output as a string. Failure is a RuntimeError.
    Calls seen: ('build', '-t', <tag>, '-f', <dockerfile>, ['--build-arg', 'K=V', ...],
    <context>); an existence check of an image,
    ('image', 'inspect', '--format', '{{.Id}}', <tag>), which the fake answers with a
    string when the image exists locally and with a RuntimeError when it does not;
    ('create', '--name', <container name>, ..., <tag>, ...); 'cp', 'start', 'wait' (the
    fake returns '0'), 'logs', 'rm'. The helpers async_context, run_sync and
    _archive_workspace run as they are."""
    ...
async def run_tool(role: str, session_id: str, workspace: Path, command: str, report: str, log_path: Path, *, image_build: AgentImageBuild, workdir: str='review', timeout: int=600) -> str:
    """Run a trusted command in the role's agent image and return its report; the report
    is read by the fake docker's 'cp' of '<container>:<report>' to the destination path.
    The image tag is '<role with _ as ->-agent:' plus a hash of the role-independent build
    inputs: the Dockerfile's contents, the contents of the files of the build context and
    the build args. It does not depend on session_id: two sessions with identical build
    inputs get the same tag, and a changed Dockerfile, context file content or build arg
    gives a different tag. When an 'image inspect' of that tag succeeds, no 'build' call
    is made and the container is created from that tag; otherwise it is built first. The
    container's '--name' is '<role with _ as ->-tool-<sha256 hex of session_id>-<unique
    suffix>' so it can be traced to its session and two runs never share a name. Images
    under the old per-session tag are never removed (no 'rmi' or 'image rm') or used.
    async_context, run_sync and _archive_workspace run as they are."""
    ...
async def run_script(role: str, session_id: str, workspace: Path, command: str, log_path: Path, *, image_build: AgentImageBuild, workdir: str, timeout: int) -> int | None:
    """Run a trusted script in the role's agent image; returns its exit status (the fake
    docker's 'wait' output). The image tag is formed, reused or built, and old images are
    left alone (async_context, run_sync and _archive_workspace run as they are), exactly as for run_tool. The container's '--name' is
    '<role with _ as ->-script-<sha256 hex of session_id>-<unique suffix>'. The 'logs'
    call is made through a real subprocess of the docker CLI and a test must make it
    harmless, for example by putting a fake 'docker' executable first on PATH."""
    ...
