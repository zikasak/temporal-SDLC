"""The agent image holds only the modules its Dockerfile copies: an agent script that
imports any other fails as its container starts, before it writes a word of output."""
import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
DOCKERFILE = BACKEND / 'app/agent_workflows/Dockerfile'
AGENTS = sorted(BACKEND.glob('app/*/agent.py'))


def _copied() -> set[str]:
    """The `app` modules the image holds, dotted, from the COPY lines of the build
    context (the agent script itself is copied as /opt/agent.py, outside the package)."""
    modules: set[str] = set()
    for line in DOCKERFILE.read_text().splitlines():
        words = line.split()
        if not words or words[0] != 'COPY' or any(w.startswith('--from') for w in words):
            continue
        for source in words[1:-1]:
            if '${' in source:
                continue
            path = BACKEND / source
            files = sorted(path.rglob('*.py')) if path.is_dir() else [path]
            for file in files:
                if file.suffix == '.py':
                    parts = file.relative_to(BACKEND).with_suffix('').parts
                    modules.add('.'.join(parts[:-1] if parts[-1] == '__init__' else parts))
    return modules


def _top_level(tree: ast.Module):
    """The statements run on import: the module's own, and those of its top-level if,
    try and with blocks, never a function's (a deferred import some host-only function
    makes, as saved_conversations.get_store does)."""
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            for field in ('body', 'orelse', 'finalbody'):
                pending += getattr(node, field, [])
            pending += [stmt for handler in getattr(node, 'handlers', [])
                        for stmt in handler.body]


def _imports(file: Path) -> set[str]:
    """The `app` modules `file` imports as it is imported."""
    names: set[str] = set()
    for node in _top_level(ast.parse(file.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            # `from app.x import y` may name the module app.x.y.
            names.update(f'{node.module}.{alias.name}' for alias in node.names
                         if (BACKEND / node.module.replace('.', '/') / f'{alias.name}.py').is_file())
    return {name for name in names if re.match(r'app(\.|$)', name)}


@pytest.mark.parametrize('agent', AGENTS, ids=lambda path: path.parent.name)
def test_an_agent_imports_only_what_its_image_holds(agent):
    copied = _copied()
    missing: set[str] = set()
    seen: set[Path] = set()
    pending = [agent]
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        for name in _imports(file):
            if name not in copied:
                missing.add(f'{name} (imported by {file.relative_to(BACKEND)})')
                continue
            module = BACKEND / name.replace('.', '/')
            pending.append(module / '__init__.py' if module.is_dir() else module.with_suffix('.py'))
    assert not missing, f'{DOCKERFILE.relative_to(BACKEND)} does not copy: {sorted(missing)}'
