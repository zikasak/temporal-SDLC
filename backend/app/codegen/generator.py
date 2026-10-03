"""Runs openapi-generator to turn an OpenAPI document into Java sources."""

import asyncio
import logging
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path

from app.config import CODEGEN_BASE_PACKAGE, OPENAPI_GENERATOR_CMD

logger = logging.getLogger(__name__)

GENERATOR_TIMEOUT_SECONDS = 240
SPRING_SERVER_PROPERTIES = {
    "interfaceOnly": "true",
    "useSpringBoot3": "true",
    "useTags": "true",
    "skipDefaultInterface": "true",
    "openApiNullable": "false",
    "documentationProvider": "none",
}


INTERFACE = re.compile(r"^public interface (\w+)", re.MULTILINE)
OPERATION = re.compile(r"^    ResponseEntity<.*>\s+(\w+)\($", re.MULTILINE)
TEST_STUB = """package {package};

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * Tests for {{@link {interface}}}. Enable each test once its operation is implemented.
 */
class {interface}Test {{
{methods}}}
"""
TEST_METHOD = """
    @Test
    @Disabled("Not implemented yet")
    void {operation}() {{
    }}
"""


class CodegenError(Exception):
    pass


def _write_api_test_stubs(project: Path) -> None:
    """One JUnit class per generated API interface, one disabled test per operation."""
    package = f"{CODEGEN_BASE_PACKAGE}.api"
    relative = Path(*package.split("."))
    tests = project / "src" / "test" / "java" / relative
    tests.mkdir(parents=True, exist_ok=True)
    for source in sorted((project / "src" / "main" / "java" / relative).glob("*Api.java")):
        text = source.read_text(encoding="utf-8")
        interface = INTERFACE.search(text)
        if not interface:
            continue
        methods = "".join(TEST_METHOD.format(operation=name) for name in OPERATION.findall(text))
        (tests / f"{interface.group(1)}Test.java").write_text(
            TEST_STUB.format(package=package, interface=interface.group(1), methods=methods),
            encoding="utf-8",
        )


def _count_files(root: Path) -> int:
    return sum(1 for item in root.rglob("*") if item.is_file())


async def _run_generator(args: list[str]) -> None:
    command = [*shlex.split(OPENAPI_GENERATOR_CMD), *args]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(), timeout=GENERATOR_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise CodegenError("openapi-generator timed out.") from exc
    if process.returncode != 0:
        tail = output.decode("utf-8", "replace").strip()[-2000:]
        raise CodegenError(f"openapi-generator exited with {process.returncode}: {tail}")


async def generate_spring_server(spec_text: str, target: Path) -> int:
    """Generate Spring API interfaces and models into target, replacing what
    was there. Returns the number of files written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as work:
        spec = Path(work) / "openapi.yaml"
        spec.write_text(spec_text, encoding="utf-8")
        output = Path(work) / "server"
        properties = ",".join(f"{key}={value}" for key, value in SPRING_SERVER_PROPERTIES.items())
        await _run_generator([
            "generate", "-g", "spring", "-i", str(spec), "-o", str(output),
            "--api-package", f"{CODEGEN_BASE_PACKAGE}.api",
            "--model-package", f"{CODEGEN_BASE_PACKAGE}.model",
            "--invoker-package", CODEGEN_BASE_PACKAGE,
            "--additional-properties", properties,
        ])
        for metadata in output.glob(".openapi-generator*"):
            if metadata.is_dir():
                shutil.rmtree(metadata)
            else:
                metadata.unlink()
        _write_api_test_stubs(output)
        # Swap in the new tree so a regeneration never mixes old and new files.
        if target.exists():
            shutil.rmtree(target)
        os.replace(output, target)
    files = _count_files(target)
    logger.info("Generated %d Spring server files in %s", files, target)
    return files
