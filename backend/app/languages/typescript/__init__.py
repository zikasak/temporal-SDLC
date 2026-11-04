"""TypeScript: the language of a part of a repository beside the service, such as a web
client, whose criteria component contracts describe at seams of its own code."""

from app.languages import PartLanguage, register_part
from app.languages.typescript.components import COMPONENT_CONTRACT, detect
from app.languages.typescript.suite import COMPONENT_SUITE

TYPESCRIPT = register_part(PartLanguage(
    name="typescript", label="TypeScript",
    detect=detect,
    build_dirs=("node_modules", "dist", "build", "coverage", ".vite", ".next", ".turbo"),
    component_contract=COMPONENT_CONTRACT, component_suite=COMPONENT_SUITE,
))
