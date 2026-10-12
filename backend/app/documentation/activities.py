"""Documentation policies: the author is shown the repository's documentation files and
nothing else of it, and nothing else is taken back."""
import dataclasses
import json
from functools import partial
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent_workflows.contracts import on_demand_contracts, write_contracts
from app.agent_workflows.host import (
    StageInputs, StageSpec, copy_regular_files, run_stage, session_language, write_language,
)
from app.config import GENERATED_CODE_DIR
from app.docker_agents import default_agent_image
from app.github.checkout import CHECKOUT_DIR, CONTRACTS_DIR, TESTS_DIR
from app.languages import Language
from app.requirements.document import without_repeated_tables

# What counts as documentation, wherever in the repository it lies.
SUFFIXES = ('.md', '.markdown', '.rst', '.adoc')
EXCLUDED = {'.git', '__pycache__', '.agent-output'}
WORKSPACE = 'repository'


def is_documentation_file(language: Language, relative: Path) -> bool:
    """Documentation only: never build output, the contract tests' own README or the
    published contract, whose requirements document is Markdown too."""
    excluded = EXCLUDED | set(language.build_dirs)
    return (relative.suffix.lower() in SUFFIXES
            and relative.parts[0] not in (TESTS_DIR, CONTRACTS_DIR)
            and not any(part.lower() in excluded for part in relative.parts))


def copy_documentation(language: Language, source: Path, target: Path) -> None:
    copy_regular_files(source, target, include=partial(is_documentation_file, language))


def changed_files(language: Language, checkout: Path, written: Path) -> list[str]:
    """The documentation files `written` adds to, changes in or removes from the checkout."""
    before, after = ({
        item.relative_to(root).as_posix(): item.read_bytes() for item in root.rglob('*')
        if is_documentation_file(language, item.relative_to(root))
        and item.is_file() and not item.is_symlink()
        and not any(parent.is_symlink() for parent in item.parents)
    } for root in (checkout, written))
    return sorted(name for name in before.keys() | after.keys()
                  if before.get(name) != after.get(name))


def prepare_inputs(language: Language, session_dir: Path, context: dict[str, Any]) -> StageInputs:
    checkout = session_dir / CHECKOUT_DIR
    if not context.get('repository') or not checkout.is_dir():
        raise ApplicationError('The repository checkout is unavailable', non_retryable=True)
    if not context.get('brief'):
        raise ApplicationError('The documentation task has no brief', non_retryable=True)

    context = without_repeated_tables(context)
    index, files = on_demand_contracts(context, len(json.dumps(context))) or (context, {})

    def populate(workspace: Path) -> None:
        copy_documentation(language, checkout, workspace / WORKSPACE)
        write_language(workspace, language, suffixes=', '.join(SUFFIXES))
        if files:
            write_contracts(workspace, files)

    return StageInputs(context=index, populate=populate)


def validate_output(language: Language, checkout: Path, clean: Path) -> None:
    if not changed_files(language, checkout, clean):
        raise RuntimeError('The documentation author changed no documentation file')


def stage(language: Language, checkout: Path) -> StageSpec:
    return StageSpec(
        role='documentation', artifact_subdir='documentation', workspace_name=WORKSPACE,
        output_subdir='files', heartbeat='Writing documentation',
        # Documentation is published as files; there is nothing to build.
        image_label=None,
        image_build=dataclasses.replace(default_agent_image('documentation', WORKSPACE),
                                        dockerfile=language.agent_dockerfile),
        prepare_inputs=partial(prepare_inputs, language),
        sanitize_output=partial(copy_documentation, language),
        validate_output=partial(validate_output, language, checkout),
    )


@activity.defn
async def write_documentation(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
    language = session_language(context.get('language'))
    checkout = GENERATED_CODE_DIR / session_id / CHECKOUT_DIR
    result = await run_stage(stage(language, checkout), session_id, context, GENERATED_CODE_DIR)
    return {**result,
            'changed_files': changed_files(language, checkout, Path(result['path']))}
