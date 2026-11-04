"""The parts of a repository whose criteria a component contract can describe: the service,
in the session's language, and the projects beside it, such as a web client, in a
registered part language (plans/part-component-contracts.md)."""
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath

from app.agent_workflows.parts import repository_parts
from app.github.checkout import COMPONENTS_DIR, CONTRACTS_DIR
from app.implementation.activities import is_service_file
from app.languages import ComponentContract, Language, PartLanguage, part_languages

# The part a seam is in when it names none.
SERVICE_PART = 'service'
# Where the contracts of the other parts live: contracts/parts/P/components/ in the
# repository, parts/P/components/ beside the routing and in the routing agent's workspace.
PARTS_DIR = 'parts'


@dataclass(frozen=True)
class ComponentPart:
    # SERVICE_PART, or the part's directory in the repository, which is its name.
    name: str
    # The part's directory in the repository (`.` for the service).
    root: str
    # The service's Language, or the part's PartLanguage.
    language: Language | PartLanguage

    @property
    def is_service(self) -> bool:
        return self.name == SERVICE_PART

    @property
    def contract(self) -> ComponentContract | None:
        return self.language.component_contract

    def contract_dir(self) -> PurePosixPath:
        """Where the part's component contract lives, relative to the routing's directory
        (and to `contracts/` in the repository)."""
        return (PurePosixPath(COMPONENTS_DIR) if self.is_service
                else PurePosixPath(PARTS_DIR, self.name, COMPONENTS_DIR))

    def published_dir(self) -> PurePosixPath:
        """Where the part's component contract is published in the repository."""
        return PurePosixPath(CONTRACTS_DIR) / self.contract_dir()

    def describe(self) -> dict[str, str]:
        """What the routing agent is told of the part."""
        return {'name': self.name, 'root': self.root, 'language': self.language.label}


def component_parts(language: Language, checkout: Path) -> list[ComponentPart]:
    """The service, then every other project of the checkout that a registered part
    language detects, by its directory. A project inside another such part or inside the
    service's sources is left out, as is one without a part language."""
    parts = [ComponentPart(SERVICE_PART, '.', language)]
    sources = set(language.implementation.contract_paths)
    found = repository_parts(checkout, partial(is_service_file, language))
    for item in found:
        path = PurePosixPath(item['path'])
        if (item['path'] in ('.', SERVICE_PART) or path.parts[0] in sources
                or any(path.is_relative_to(part.root) for part in parts[1:])):
            continue
        detected = next((candidate for candidate in part_languages()
                         if candidate.detect(checkout / path)), None)
        if detected is not None:
            parts.append(ComponentPart(path.as_posix(), path.as_posix(), detected))
    return parts
