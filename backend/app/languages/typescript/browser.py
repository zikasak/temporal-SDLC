"""Browser tests of TypeScript parts that build a web page with Vite: the page contract's
conformance runner and the Playwright suite, whose test identities are read on the host
from the test files' tokens, one per viewport a test is tagged with
(plans/browser-route.md)."""

import re
from pathlib import Path

from app.images import PLAYWRIGHT_IMAGE, PLAYWRIGHT_VERSION
from app.languages import BrowserContract, BrowserSuite, TOOLS_DIR
from app.languages.typescript.components import _Token, _manifest
from app.languages.typescript.suite import _literal, _matches, _scan

CONTRACT_RUNNER = Path(__file__).parent / "browser_contract"
CHECK_TOOL = CONTRACT_RUNNER / "check-pages.mjs"
TEMPLATE = Path(__file__).parent / "browser_suite"
TOOLS = f"/workspace/{TOOLS_DIR}"
TEST_GLOB = "tests/**/*.spec.ts"
TEST_SUFFIX = ".spec.ts"
SUPPORT = "tests/support"
MAX_TEST_FILE_BYTES = 1024 * 1024
# A viewport's name, as viewports.json and the tests' tags give it.
VIEWPORT_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

CONTRACT_INSTRUCTIONS = """Page contract (browser route) of a part: it lives in
/workspace/routing/parts/<part>/browser/, which already holds the part's published page
contract (its contracts/parts/<part>/browser/), if there is one. Keep every published
file: you may add pages, elements, actions, operations, fixtures and viewports, but never
change or remove one that is published. The test author sees only this contract, never
the client's code, and tests the page as a user would, through the elements it declares.
- pages.json: {"api_prefix": "/api", "pages": [{"entry": "/…", "operations": ["…"],
  "elements": [ELEMENT, …], "actions": [{"target": ELEMENT, "input": "reveal" | "hover" |
  "tap" | "click" | "focus", "shows": [ELEMENT, …], "operations": ["…"]}]}]}.
  `api_prefix` is the path below which the client calls the API (as its dev server's
  proxy or its fetch calls show), "" when it calls the API's paths as they are. `entry` is
  the path within the client the page opens, starting with "/". `operations` are the
  `operationId`s of contracts/openapi.yaml the page calls when it loads, and an action's
  those it calls after it. `elements` are those present once the page is idle after
  loading; an action's `shows` those present after it (such as a popup shown on hover),
  `reveal` meaning hover with a mouse and tap on a touch screen. An ELEMENT is
  {"role": "<ARIA role>", "name": "<accessible name>"} (the name optional) or
  {"testid": "<data-testid>"}: how a test finds it.
- fixtures/<operationId>.json, and fixtures/<operationId>.<case>.json for other cases of
  it: {"status": <HTTP status>, "body": <JSON body or null>}, a response the API may give,
  valid against that operation's response schema in contracts/openapi.yaml for that
  status (the host checks it). Every operation a page or action names needs its default
  fixtures/<operationId>.json, with data that makes the declared elements appear. When
  {tools}/operations.json is empty, the delivery has no API contract, and a page may
  call no operation: then route to it only a page that calls none.
- viewports.json: {"<name>": {"width": …, "height": …, "input": "mouse" | "touch",
  "scale": 1}}, the window sizes and inputs the browser tests run in. Names are lowercase
  words; width 280 to 3840, height 400 to 2160, scale 1 to 3 (default 1). There is no
  default set: define those the criteria need. Read the client's CSS (@media rules) for
  the breakpoint a bug is tied to and put a viewport on the side the bug is on, and on the
  other side only as a guard when the fix could move the bug there. Common sizes are
  1280×800 with a mouse, 390×844 and 768×1024 with touch. Every viewport must be named by
  some route, and a published viewport may not change: a different size is a new name.
- A route's `page.roles` are elements of the page, from `elements` or an action's
  `shows`, that its test finds. An element the client lacks yet, such as a role or a
  data-testid on an element that already exists, goes in `page.added` as well: the change
  adds it first, with no other change.
- Check the contract before you finish, if a browser can start in your workspace: copy
  the part out of the repository (cp -r /workspace/routing/repository/<part> /tmp/part),
  run npm ci --ignore-scripts and node_modules/.bin/vite build --outDir /tmp/site in
  /tmp/part, then in a directory of your own run npm install @playwright/test@1.65.1 and
  npx playwright install chromium, and from there node {tools}/check-pages.mjs
  --contract /workspace/routing/parts/<part>/browser --site /tmp/site --operations
  {tools}/operations.json. It must report no error but, for each element in a route's
  `added`, that it is not present. The host runs the same check either way."""

SUITE_INSTRUCTIONS = """The browser tests of a TypeScript part are a Playwright project
run in headless Chromium against the part's built web client, written from the page
contract alone: the routing's `page` of each criterion names the page (`entry`), the
viewports and the elements (`roles`) the test uses, and the contract is in
/workspace/contracts/parts/<part>/browser (pages.json, fixtures/, viewports.json). Write
test files named *.spec.ts under tests/ (never in tests/support). Each imports `test`,
`expect`, `fixture`, `reveal`, `expectFullyVisible`, `expectInViewport` and `settle` from
'./support' (a path relative to the test file), never `test` from '@playwright/test'.
- Before page.goto, answer every operation the page calls (pages.json lists them) with
  `await fixture(page, '<operationId>')`, or `fixture(page, '<operationId>', '<case>')`
  for fixtures/<operationId>.<case>.json. A request the test did not answer fails it.
- Find elements as pages.json declares them: page.getByRole(role, { name, exact: true })
  or page.getByTestId(testid). Show what an action reveals with `await reveal(locator)`,
  which hovers with a mouse and taps on a touch screen, so one test covers both inputs.
- Assert layout only with the helpers: expectFullyVisible(locator) (inside the window and
  on top, not cut off or covered, at its corners and centre) and
  expectInViewport(locator); call `await settle(page)` before them. Never compute
  geometry in a test.
- Tag every test with the viewports its criterion's route names, as string literals:
  test('…', { tag: ['@desktop', '@narrow'] }, async ({ page }) => { … }), or on the
  test.describe block around it. Each viewport's run takes only the tests tagged with its
  name, so a test without a tag, or with a tag that is not a viewport of viewports.json,
  cannot be tracked and is rejected.
- Name every test and every test.describe block with a string literal without `@`: no
  name computed from an expression and no test generated in a loop.
List any further dependency of the tests in package.json's devDependencies and update
package-lock.json with npm install --ignore-scripts; never list the part or its own
dependencies. The part's code is not available here, so the suite cannot be run.
Type-check it instead, from the suite's directory: npm ci --ignore-scripts, then npx tsc
-p tsconfig.json, and fix every error."""


def detect_browser(part: Path) -> bool:
    """A web client Vite builds: `vite` among the dependencies and an index.html at the
    part's root."""
    page = part / "index.html"
    if page.is_symlink() or not page.is_file():
        return False
    manifest = _manifest(part / "package.json")
    return any(isinstance(manifest.get(key), dict) and "vite" in manifest[key]
               for key in ("dependencies", "devDependencies"))


def build_args(project: str) -> dict[str, str]:
    """How the conformance and test runners install and build the part, and on what."""
    return {"PROJECT": project, "BASE_IMAGE": PLAYWRIGHT_IMAGE}


def _runner_build_args(runtime: dict) -> dict[str, str]:
    # The suite's runner also holds the suite to the image's release of Playwright.
    return {**build_args(runtime["project"]), "PLAYWRIGHT_VERSION": PLAYWRIGHT_VERSION}


# The calls of `test` a test file may make: a test, perhaps through a modifier, a describe
# block, and calls that declare neither.
TEST_MODIFIERS = {"only", "skip", "fixme", "fail", "slow"}
DESCRIBE_MODIFIERS = {"only", "skip", "fixme", "serial", "parallel"}


class _Cases:
    """The tests of a Playwright suite by identity, each with its source, and what of it
    could not be tracked."""

    def __init__(self) -> None:
        self.cases: dict[str, str] = {}
        self.untracked: list[str] = []

    def _tags(self, tokens: list[_Token], matches: dict[int, int],
              start: int) -> tuple[list[str], str | None]:
        """The tags of the details object whose `{` is at `start`, or why they cannot be
        read."""
        end = matches.get(start, start)
        position, tags = start + 1, []
        while position < end:
            token = tokens[position]
            if token.value == "...":
                return [], "its details spread an object"
            name = _literal(token) if token.kind == "string" else (
                token.value if token.kind == "name" else None)
            if name is None or tokens[position + 1].value != ":":
                if name == "tag":
                    return [], "its tag is not given as a string literal"
                # Skip whatever this entry is up to the next top-level comma.
                while position < end and tokens[position].value != ",":
                    position = matches.get(position, position) + 1
                position += 1
                continue
            value = position + 2
            if name == "tag":
                if tokens[value].value == "[" and tokens[value].kind == "op":
                    close = matches[value]
                    items = [item for item in tokens[value + 1:close] if item.value != ","]
                    literals = [_literal(item) for item in items]
                    commas = [item for item in tokens[value + 1:close] if item.value == ","]
                    if (any(item is None for item in literals)
                            or len(commas) not in (len(items) - 1, len(items))):
                        return [], "its tag is not given as string literals"
                    tags += [item for item in literals if item is not None]
                    after = close + 1
                else:
                    literal = _literal(tokens[value])
                    if literal is None:
                        return [], "its tag is not given as a string literal"
                    tags.append(literal)
                    after = value + 1
                if tokens[after].value not in (",", "}"):
                    return [], "its tag is not given as a string literal"
                position = after + 1
                continue
            while position < end and tokens[position].value != ",":
                position = matches.get(position, position) + 1
            position += 1
        for tag in tags:
            if not tag.startswith("@") or not VIEWPORT_NAME.fullmatch(tag[1:]):
                return [], (f"its tag {tag[:40]!r} is not @ and a viewport's name in lowercase "
                            "words")
        return tags, None

    def read(self, file: str, source: str) -> None:
        try:
            tokens, _ = _scan(source, 0, False)
            matches = _matches(tokens)
        except ValueError as exc:
            self.untracked.append(f"{file} cannot be read: {exc}")
            return
        # The describe blocks around the position: the index of their closing `)`, their
        # name (None for one that cannot be tracked), their tags and their call's head.
        scopes: list[tuple[int, str | None, list[str], str]] = []
        for index, token in enumerate(tokens):
            while scopes and scopes[-1][0] < index:
                scopes.pop()
            if (token.kind != "name" or token.value != "test"
                    or index and tokens[index - 1].value in (".", "function", "?.")):
                continue
            chain, position = [], index + 1
            while (position + 1 < len(tokens) and tokens[position].value == "."
                   and tokens[position + 1].kind == "name"):
                chain.append(tokens[position + 1].value)
                position += 2
            if position >= len(tokens) or tokens[position].value != "(":
                continue
            opening = position
            if chain[:1] == ["describe"]:
                if any(item not in DESCRIBE_MODIFIERS for item in chain[1:]):
                    continue  # test.describe.configure and the like declare no block
                what = "describe block"
            elif not chain or (len(chain) == 1 and chain[0] in TEST_MODIFIERS):
                what = "test"
            else:
                continue  # test.step, test.use, test.beforeEach, test.expect…
            end = matches[opening]
            first = tokens[opening + 1] if opening + 1 < end else None
            name = _literal(first) if first is not None else None
            after = tokens[opening + 2].value if opening + 2 <= end else None
            if name is not None and after not in (",", ")"):
                name = None  # a literal in an expression, such as `"a" + b`
            if what == "test" and chain and name is None:
                # test.skip(condition) and the like inside a test or a describe block: an
                # annotation, not a test.
                continue
            tracked = all(scope[1] is not None for scope in scopes)
            tags, problem = [], None
            details = opening + 3
            if (name is not None and after == "," and details < end
                    and tokens[details].value == "{" and tokens[details].kind == "op"):
                tags, problem = self._tags(tokens, matches, details)
            head = " ".join(item.value for item in tokens[index:opening + 2])
            if tracked and name is None:
                self.untracked.append(
                    f"{file} line {token.line}: a {what} named by an expression cannot be "
                    "tracked; name it with a string literal")
            elif tracked and name is not None and "@" in name:
                self.untracked.append(
                    f"{file} line {token.line}: the {what} {name[:80]!r} has `@` in its "
                    "name, which marks only viewport tags")
                name = None
            elif tracked and name is not None and problem:
                self.untracked.append(f"{file} line {token.line}: the {what} {name[:80]!r} "
                                      f"cannot be tracked: {problem}")
                name = None
            if what == "describe block":
                scopes.append((end, name, tags, head))
                continue
            if name is None or not tracked:
                continue
            viewports = list(dict.fromkeys([*(tag for scope in scopes for tag in scope[2]),
                                            *tags]))
            if not viewports:
                self.untracked.append(
                    f"{file} line {token.line}: the test {name[:80]!r} has no viewport tag; "
                    "tag it, or its describe block, with { tag: ['@<viewport>'] }")
                continue
            names = [scope[1] for scope in scopes if scope[1] is not None]
            text = " ".join([*(scope[3] for scope in scopes),
                             *(item.value for item in tokens[index:end + 1])])
            for tag in viewports:
                case = f"{file}#{tag[1:]} > {' > '.join([*names, name])}"
                self.cases[case] = f"{self.cases[case]} {text}" if case in self.cases else text


def _read(project: Path) -> _Cases:
    cases = _Cases()
    for file in sorted(project.glob(TEST_GLOB)):
        relative = file.relative_to(project).as_posix()
        if (not file.name.endswith(TEST_SUFFIX) or file.is_symlink() or not file.is_file()
                or relative.startswith(f"{SUPPORT}/")):
            continue
        if file.stat().st_size > MAX_TEST_FILE_BYTES:
            cases.untracked.append(f"{relative} is too large to read")
            continue
        cases.read(relative, file.read_text(encoding="utf-8", errors="replace"))
    return cases


def _test_cases(project: Path) -> dict[str, str]:
    """Every test of the project, once per viewport it is tagged with, by
    `tests/a.spec.ts#narrow > Outer > test`: the identity the runner's JUnit report gives
    it, its class name the file and the viewport, its name the describe blocks' names and
    its own. Its source is the call's tokens, after the heads of the describe calls around
    it, so that layout and comments are not a change."""
    return _read(project).cases


def _untracked_tests(project: Path) -> list[str]:
    return _read(project).untracked


def _case_id(class_name: str, name: str) -> str:
    # The runner's reporter: `tests/a.spec.ts#narrow` and `Outer > test`.
    return f"{class_name} > {name}"


BROWSER_CONTRACT = BrowserContract(
    instructions=CONTRACT_INSTRUCTIONS.replace("{tools}", TOOLS),
    conformance=CONTRACT_RUNNER, build_args=build_args, tools=(CHECK_TOOL,),
)

BROWSER_SUITE = BrowserSuite(
    template=TEMPLATE,
    instructions=SUITE_INSTRUCTIONS,
    published=("tests", "package.json", "package-lock.json", "tsconfig.json", "README.md"),
    test_glob=TEST_GLOB, test_cases=_test_cases, case_id=_case_id,
    untracked_tests=_untracked_tests,
    required=("package.json", "package-lock.json"),
    runner=("Dockerfile", ".dockerignore", "run-tests.sh", "playwright.config.mjs",
            "reporter.mjs", "serve.mjs"),
    build_args=_runner_build_args,
    trusted=(f"{SUPPORT}/index.ts",),
)
