"""The projects a repository holds, found by their build manifests: the service may be one
part of several, such as a backend beside a web frontend, each in its own language."""
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Build manifest → the toolchain of the project in its directory.
MANIFESTS = {
    'package.json': 'Node.js (npm)',
    'pyproject.toml': 'Python', 'requirements.txt': 'Python', 'setup.py': 'Python',
    'pom.xml': 'Java (Maven)', 'build.gradle': 'Gradle', 'build.gradle.kts': 'Gradle',
    'go.mod': 'Go', 'Cargo.toml': 'Rust', 'Gemfile': 'Ruby', 'composer.json': 'PHP',
    'pubspec.yaml': 'Dart (Flutter)',
}
# How many directories below the root a project is looked for: `frontend/` or `apps/web/`,
# but not the fixtures and templates deeper inside a project.
MAX_DEPTH = 2
MAX_PARTS = 20


def repository_parts(root: Path, include: Callable[[Path], bool]) -> list[dict[str, Any]]:
    """Each directory, relative to `root` (`.` for the root itself), holding a build
    manifest that `include` accepts, with its toolchain, in path order."""
    found: dict[str, list[str]] = {}
    directories = [Path()]
    for _ in range(MAX_DEPTH + 1):
        nested = []
        for directory in directories:
            for item in sorted((root / directory).iterdir()):
                relative = directory / item.name
                if item.is_symlink() or not include(relative):
                    continue
                if item.is_dir():
                    nested.append(relative)
                elif item.name in MANIFESTS:
                    toolchains = found.setdefault(directory.as_posix(), [])
                    if MANIFESTS[item.name] not in toolchains:
                        toolchains.append(MANIFESTS[item.name])
        directories = nested
    return [{'path': path, 'toolchain': ', '.join(toolchains)}
            for path, toolchains in sorted(found.items())][:MAX_PARTS]
