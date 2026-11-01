"""Host-only choice between embedding the contracts and supplying them on demand."""
import json
from pathlib import Path
from typing import Any

from app.api_design.openapi import DOCUMENT_FILENAME as OPENAPI_FILENAME
from app.api_design.openapi import endpoint_key, render_openapi
from app.config import CONTRACT_EMBED_CHARS

# Beside the agent's project, never inside it: the repository's own copy is written
# from the task when it is published, never from a workspace.
CONTRACTS_DIR = 'contracts'
REQUIREMENTS_FILENAME = 'requirements.json'
NARRATIVE_FILENAME = 'requirements.md'
ARCHITECTURE_FILENAME = 'architecture.md'
CONTRACT_KEYS = {'requirements', 'requirements_document', 'requirements_narrative', 'api_design',
                 'architecture'}
FILE_PURPOSES = {
    ARCHITECTURE_FILENAME: ('The top-level architecture: components, data flow and technology '
                            'choices'),
    REQUIREMENTS_FILENAME: 'Every requirement with its description and rationale',
    OPENAPI_FILENAME: 'The complete API contract',
    NARRATIVE_FILENAME: 'Business context and assumptions beyond the requirement list',
}
GUIDE = ('The contracts are too large to include here. This index lists every requirement, '
         'endpoint and schema; the complete text is in the read-only contract files. Before '
         'working on a requirement or endpoint, find it with search_contracts and read its '
         'full definition with read_contract. Never rely on a title or summary alone.')


def on_demand_contracts(context: dict[str, Any], embedded_chars: int,
                        ) -> tuple[dict[str, Any], dict[str, str]] | None:
    """The index to show the model and the contract files to supply beside it.

    None while the contracts are small enough to embed whole, which costs no extra
    reads. `context` holds the stage's requirements and the structured API design.
    """
    api = context.get('api_design')
    if embedded_chars <= CONTRACT_EMBED_CHARS or not api:
        return None
    requirements = context.get('requirements') or []
    files = {
        REQUIREMENTS_FILENAME: json.dumps(requirements, ensure_ascii=False, indent=2),
        OPENAPI_FILENAME: render_openapi(api),
    }
    narrative = context.get('requirements_narrative') or context.get('requirements_document')
    if narrative:
        files[NARRATIVE_FILENAME] = narrative
    if context.get('architecture'):
        files[ARCHITECTURE_FILENAME] = context['architecture']
    index = {
        **{key: value for key, value in context.items() if key not in CONTRACT_KEYS},
        'contracts': GUIDE,
        'contract_files': {name: FILE_PURPOSES[name] for name in files},
        'requirements': [{key: item[key] for key in ('id', 'category', 'priority', 'title')}
                         for item in requirements],
        'api': {**{key: api['info'][key] for key in ('title', 'auth')},
                'endpoints': [{'operation': endpoint_key(endpoint),
                               'summary': endpoint['summary'],
                               'requirements': endpoint['requirement_ids']}
                              for endpoint in api['endpoints']],
                'schemas': [schema['name'] for schema in api['schemas']]},
    }
    return index, files


def write_contracts(workspace: Path, files: dict[str, str]) -> None:
    directory = workspace / CONTRACTS_DIR
    directory.mkdir()
    for name, text in files.items():
        (directory / name).write_text(text)
