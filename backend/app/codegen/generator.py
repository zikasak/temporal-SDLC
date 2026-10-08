"""Runs openapi-generator to turn an OpenAPI document into a language's server code."""

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from app.languages import Language
from app.config import OPENAPI_GENERATOR_CMD

logger = logging.getLogger(__name__)

GENERATOR_TIMEOUT_SECONDS = 240


class CodegenError(Exception):
    pass


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


async def generate_server(language: Language, spec_text: str, target: Path) -> int:
    """Generate the language's server code into target, replacing what was there.
    Returns the number of files written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as work:
        spec = Path(work) / "openapi.yaml"
        spec.write_text(spec_text, encoding="utf-8")
        output = Path(work) / "server"
        await _run_generator([
            "generate", "-i", str(spec), "-o", str(output), *language.generator_args(),
        ])
        for metadata in output.glob(".openapi-generator*"):
            if metadata.is_dir():
                shutil.rmtree(metadata)
            else:
                metadata.unlink()
        language.post_process(output)
        # Swap in the new tree so a regeneration never mixes old and new files.
        if target.exists():
            shutil.rmtree(target)
        os.replace(output, target)
    files = _count_files(target)
    logger.info("Generated %d %s server files in %s", files, language.label, target)
    return files
