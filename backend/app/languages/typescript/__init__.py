"""TypeScript: the language of a part of a repository beside the service, such as a web
client, whose criteria component contracts describe at seams of its own code, and, for a
web client Vite builds, page contracts at its pages in a real browser."""

from app.languages import PartLanguage, register_part
from app.languages.static_checks import ESLINT
from app.languages.typescript.browser import (
    BROWSER_CONTRACT, BROWSER_SUITE, detect_browser,
)
from app.languages.typescript.components import COMPONENT_CONTRACT, detect
from app.languages.typescript.suite import COMPONENT_SUITE

TYPESCRIPT = register_part(PartLanguage(
    name="typescript", label="TypeScript",
    detect=detect,
    build_dirs=("node_modules", "dist", "build", "coverage", ".vite", ".next", ".turbo"),
    component_contract=COMPONENT_CONTRACT, component_suite=COMPONENT_SUITE,
    detect_browser=detect_browser,
    browser_contract=BROWSER_CONTRACT, browser_suite=BROWSER_SUITE,
    static_checks=(ESLINT,),
))
