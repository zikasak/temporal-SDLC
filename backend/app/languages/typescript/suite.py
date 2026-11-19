"""Component suites of TypeScript parts: vitest projects run in jsdom against the part's
code, and the identity of their tests, read on the host from the test files' tokens."""

import re
import shlex
from pathlib import Path

from app.languages import ComponentSuite
from app.languages.typescript.components import TOKEN, _template, _Token, runner_build_args

TEMPLATE = Path(__file__).parent / "component_suite"
# Test files: *.test.ts and *.test.tsx, as the runner's configuration includes them.
TEST_GLOB = "tests/**/*.test.ts*"
TEST_SUFFIXES = (".test.ts", ".test.tsx")
MAX_TEST_FILE_BYTES = 1024 * 1024

INSTRUCTIONS = """The suite of a TypeScript part is a vitest project run in jsdom. Write
test files named *.test.ts (or *.test.tsx) under tests/. Each imports `test` (or `it`),
`describe`, `expect` and `vi` from 'vitest', and imports the part's code as
`@part/<path within the part>` (`@part/src/github` for src/github.ts): only the stubbed
modules, and only the exports the stubs declare, called with the types they declare.
Name every test and every describe block around it with a string literal, as the tests
are tracked by those names: no name computed from an expression, and no test.each or
test.for (write one test per case instead). Fake browser APIs and collaborators the stubs
name in the suite itself, with vi.spyOn, vi.fn or vi.stubGlobal, and restore them after
each test; never fake the seam itself.
List any further dependency of the tests in package.json's devDependencies and update
package-lock.json with npm install --ignore-scripts; never list the part or its own
dependencies, which the runner installs from the part's lock file.
The part's code is not available here, so the suite cannot be run. Type-check it against
the stubs instead, from the suite's directory: ln -sfn
/workspace/contracts/parts/<part>/components .part, npm ci --ignore-scripts, then
npx tsc -p tsconfig.json, and fix every error about a stubbed symbol. The runner uses its
own vitest configuration, so leave tsconfig.json's `@part/*` path as it is."""

TEST_FUNCTIONS = {"test", "it"}
SUITE_FUNCTIONS = {"describe", "suite"}
# What a test or describe block may be called through, changing how it runs or not.
MODIFIERS = {"skip", "only", "todo", "concurrent", "sequential", "fails", "shuffle"}
CONDITIONS = {"runIf", "skipIf"}
PARAMETERIZED = {"each", "for"}
# Words an expression may follow: after them, `/` starts a regular expression and `<` an
# element of JSX, not a division or a comparison.
EXPRESSION_WORDS = {"return", "typeof", "case", "yield", "await", "in", "of", "new", "else",
                    "do", "void", "delete", "instanceof", "throw", "extends"}
JSX_NAME = re.compile(r"[A-Za-z_$][\w$.:-]*")
JSX_ATTRIBUTE = re.compile(r"[A-Za-z_$][\w$:-]*")
ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]{1,6}\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|.)", re.S)
SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
                  "0": "\0", "\n": "", "\r": "", "\r\n": "", "\u2028": "", "\u2029": ""}
OPENING = {"(": ")", "[": "]", "{": "}"}


def _expects_expression(previous: _Token | None) -> bool:
    """Whether what follows `previous` starts an expression, where `/` is a regular
    expression and `<` an element."""
    if previous is None:
        return True
    if previous.kind == "name":
        return previous.value in EXPRESSION_WORDS
    if previous.kind == "op":
        return previous.value not in (")", "]", "}")
    return False


def _regex(source: str, start: int) -> int:
    """The end of the regular expression literal at `start`, with its flags."""
    position, in_class = start + 1, False
    while position < len(source):
        char = source[position]
        if char == "\n":
            break
        if char == "\\":
            position += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            position += 1
            while position < len(source) and (source[position].isalnum()
                                              or source[position] == "_"):
                position += 1
            return position
        position += 1
    raise ValueError("unterminated regular expression")


def _expression(source: str, start: int, jsx: bool) -> int:
    """The end of the braced expression at `start`, inside an element of JSX."""
    _, end = _scan(source, start + 1, jsx, closing="}")
    return end


def _jsx(source: str, start: int) -> int:
    """The end of the element of JSX at `start`, with its children."""
    position = start + 1
    name = ""
    if source.startswith(">", position):
        position += 1
    else:
        match = JSX_NAME.match(source, position)
        if match is None:
            raise ValueError("unreadable element")
        name, position = match.group(), match.end()
        while True:
            while position < len(source) and source[position].isspace():
                position += 1
            if source.startswith("/>", position):
                return position + 2
            if source.startswith(">", position):
                position += 1
                break
            if source.startswith("{", position):
                position = _expression(source, position, True)
            elif source.startswith("<", position) and name:
                # Type arguments of a generic component.
                end = source.find(">", position)
                if end < 0:
                    raise ValueError("unterminated element")
                position = end + 1
            elif (match := JSX_ATTRIBUTE.match(source, position)) is not None:
                position = match.end()
                while position < len(source) and source[position].isspace():
                    position += 1
                if source.startswith("=", position):
                    position += 1
                    while position < len(source) and source[position].isspace():
                        position += 1
                    quote = source[position:position + 1]
                    if quote in ("'", '"'):
                        end = source.find(quote, position + 1)
                        if end < 0:
                            raise ValueError("unterminated attribute")
                        position = end + 1
                    elif quote == "{":
                        position = _expression(source, position, True)
                    elif quote == "<":
                        position = _jsx(source, position)
                    else:
                        raise ValueError("unreadable attribute")
            else:
                raise ValueError("unreadable element")
    # Children, up to the closing tag.
    while position < len(source):
        if source.startswith("</", position):
            end = source.find(">", position)
            if end < 0 or source[position + 2:end].strip() != name:
                raise ValueError(f"element {name or '<>'} is not closed")
            return end + 1
        if source.startswith("<", position):
            position = _jsx(source, position)
        elif source.startswith("{", position):
            position = _expression(source, position, True)
        else:
            position += 1
    raise ValueError(f"element {name or '<>'} is not closed")


def _scan(source: str, position: int, jsx: bool,
          closing: str | None = None) -> tuple[list[_Token], int]:
    """The tokens of code from `position`, without layout and comments, up to the end of
    the source or, with `closing`, past the bracket that closes the code. A regular
    expression and an element of JSX (`jsx`, in a .tsx file) are one token each."""
    tokens: list[_Token] = []
    depth = 0
    line = source.count("\n", 0, position) + 1
    while position < len(source):
        char = source[position]
        previous = tokens[-1] if tokens else None
        end, kind = None, None
        try:
            if char == "`":
                end, kind = _template(source, position), "template"
            elif (char == "/" and not source.startswith(("//", "/*"), position)
                  and _expects_expression(previous)):
                end, kind = _regex(source, position), "regex"
            elif (jsx and char == "<" and _expects_expression(previous)
                  and (source.startswith(">", position + 1)
                       or JSX_NAME.match(source, position + 1))):
                end, kind = _jsx(source, position), "jsx"
        except ValueError as exc:
            raise ValueError(f"line {line}: {exc}") from None
        if end is not None:
            assert kind is not None  # set with `end`
            value = source[position:end]
            # An element's layout is not a change; a template's is part of its value.
            tokens.append(_Token(kind, " ".join(value.split()) if kind == "jsx" else value,
                                 line))
            line += value.count("\n")
            position = end
            continue
        match = TOKEN.match(source, position)
        if match is None:
            raise ValueError(f"line {line}: unreadable character")
        kind, value = match.lastgroup, match.group()
        assert kind is not None  # every alternative of TOKEN is a named group
        if kind == "op" and value in "\"'":
            raise ValueError(f"line {line}: unterminated literal")
        if kind == "op" and value == "/" and source.startswith("/*", position):
            raise ValueError(f"line {line}: unterminated comment")
        line += value.count("\n")
        position = match.end()
        if kind in ("space", "comment", "doc"):
            continue
        if closing is not None and kind == "op":
            if value in OPENING:
                depth += 1
            elif value in OPENING.values():
                if depth == 0:
                    if value != closing:
                        raise ValueError(f"line {line}: unbalanced {value!r}")
                    return tokens, position
                depth -= 1
        tokens.append(_Token(kind, value, line))
    if closing is not None:
        raise ValueError(f"line {line}: {closing!r} is missing")
    return tokens, position


def _unescape(text: str) -> str:
    def replace(match: re.Match) -> str:
        escape = match.group(1)
        if escape.startswith("u{"):
            return chr(int(escape[2:-1], 16))
        if escape[0] in "ux" and len(escape) > 1:
            return chr(int(escape[1:], 16))
        return SIMPLE_ESCAPES[escape] if escape in SIMPLE_ESCAPES else escape
    value = ESCAPE.sub(replace, text)
    # Escaped surrogate pairs are one character.
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _literal(token: _Token) -> str | None:
    """The string a literal token is, or None for anything computed."""
    if token.kind == "string":
        return _unescape(token.value[1:-1])
    if token.kind == "template" and "${" not in token.value.replace("\\$", ""):
        return _unescape(token.value[1:-1].replace("\r\n", "\n"))
    return None


def _matches(tokens: list[_Token]) -> dict[int, int]:
    """The index of the closing bracket of each opening one."""
    matches: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "op":
            continue
        if token.value in OPENING:
            stack.append(index)
        elif token.value in OPENING.values():
            if not stack or OPENING[tokens[stack[-1]].value] != token.value:
                raise ValueError(f"line {token.line}: unbalanced {token.value!r}")
            matches[stack.pop()] = index
    if stack:
        raise ValueError(f"line {tokens[stack[-1]].line}: unbalanced "
                         f"{tokens[stack[-1]].value!r}")
    return matches


def _call(tokens: list[_Token], matches: dict[int, int],
          index: int) -> tuple[int, set[str]] | None:
    """When the token at `index` is the callee of a test or describe call, perhaps through
    modifiers (`test.skip(`, `test.runIf(c)(`), the index of the call's `(` and the
    modifiers it is called through."""
    position, modifiers = index + 1, set()
    while position < len(tokens):
        token = tokens[position]
        if token.value == "(" and token.kind == "op":
            return position, modifiers
        if token.value != "." or position + 1 >= len(tokens):
            return None
        word = tokens[position + 1].value
        if word not in MODIFIERS | CONDITIONS | PARAMETERIZED:
            return None
        modifiers.add(word)
        position += 2
        if word in CONDITIONS | PARAMETERIZED:
            # Called first with its condition or its table, then with the test.
            if position >= len(tokens) or tokens[position].value != "(":
                return None
            position = matches[position] + 1
    return None


class _Cases:
    """The tests of a suite by identity, each with its source, and what of it could not be
    tracked."""

    def __init__(self) -> None:
        self.cases: dict[str, str] = {}
        self.untracked: list[str] = []

    def read(self, file: str, source: str) -> None:
        try:
            tokens, _ = _scan(source, 0, file.endswith(".tsx"))
            matches = _matches(tokens)
        except ValueError as exc:
            self.untracked.append(f"{file} cannot be read: {exc}")
            return
        # The describe blocks around the position: the index of their closing `)`, their
        # name (None for one that cannot be tracked) and their call's head.
        scopes: list[tuple[int, str | None, str]] = []
        for index, token in enumerate(tokens):
            while scopes and scopes[-1][0] < index:
                scopes.pop()
            if (token.kind != "name" or token.value not in TEST_FUNCTIONS | SUITE_FUNCTIONS
                    or index and tokens[index - 1].value in (".", "function", "?.")):
                continue
            call = _call(tokens, matches, index)
            if call is None:
                continue
            opening, modifiers = call
            end = matches[opening]
            first = tokens[opening + 1] if opening + 1 < end else None
            name = _literal(first) if first is not None else None
            after = tokens[opening + 2].value if opening + 2 <= end else None
            if name is not None and after not in (",", ")"):
                name = None  # a literal in an expression, such as `"a" + b`
            what = "describe block" if token.value in SUITE_FUNCTIONS else "test"
            tracked = all(scope[1] is not None for scope in scopes)
            if tracked and modifiers & PARAMETERIZED:
                self.untracked.append(
                    f"{file} line {token.line}: a parameterized {what} "
                    f"({token.value}.{'/'.join(sorted(modifiers & PARAMETERIZED))}) cannot "
                    "be tracked; write one test per case")
                name = None
            elif tracked and name is None:
                self.untracked.append(
                    f"{file} line {token.line}: a {what} named by an expression cannot be "
                    "tracked; name it with a string literal")
            head = " ".join(item.value for item in tokens[index:opening + 2])
            if token.value in SUITE_FUNCTIONS:
                scopes.append((end, name, head))
                continue
            if name is None or not tracked:
                continue
            # Every scope has a name: the test is tracked.
            names = [scope[1] for scope in scopes if scope[1] is not None]
            case = f"{file}#{' > '.join([*names, name])}"
            text = " ".join([*(scope[2] for scope in scopes),
                             *(item.value for item in tokens[index:end + 1])])
            self.cases[case] = f"{self.cases[case]} {text}" if case in self.cases else text


def _read(project: Path) -> _Cases:
    cases = _Cases()
    for file in sorted(project.glob(TEST_GLOB)):
        if (not file.name.endswith(TEST_SUFFIXES) or file.is_symlink()
                or not file.is_file()):
            continue
        relative = file.relative_to(project).as_posix()
        if file.stat().st_size > MAX_TEST_FILE_BYTES:
            cases.untracked.append(f"{relative} is too large to read")
            continue
        cases.read(relative, file.read_text(encoding="utf-8", errors="replace"))
    return cases


def _test_cases(project: Path) -> dict[str, str]:
    """Every test of the project by `tests/a.test.ts#Outer > inner > test`, the identity
    vitest's JUnit report gives it: the file from the project, as the runner's root is
    there, and the names of the describe blocks around the test and of the test. Its
    source is the call's tokens, after the heads of the describe calls around it, so that
    layout and comments are not a change."""
    return _read(project).cases


def _untracked_tests(project: Path) -> list[str]:
    return _read(project).untracked


def _case_id(class_name: str, name: str) -> str:
    # vitest's JUnit report: the test file as the class name, and the names of the
    # describe blocks and of the test joined by " > ".
    return f"{class_name}#{name}"


def _part_command(root: str, project: str, report: str, prefix: str) -> str:
    """The runner image's command, with its environment, where the composite runner
    keeps the image's trees under `root`."""
    quote = shlex.quote
    return (f'cd {quote(root + "/tests")} && NO_COLOR=1 '
            f'PART={quote(root + "/repository/" + project)} REPORT={quote(report)} '
            f'CLASSNAME_PREFIX={quote(prefix)} node_modules/.bin/vitest run '
            f'--config {quote(root + "/runner/vitest.config.mjs")}')


COMPONENT_SUITE = ComponentSuite(
    template=TEMPLATE,
    instructions=INSTRUCTIONS,
    published=("tests", "package.json", "package-lock.json", "tsconfig.json", "README.md"),
    test_glob=TEST_GLOB, test_cases=_test_cases, case_id=_case_id,
    untracked_tests=_untracked_tests,
    required=("package.json", "package-lock.json"),
    runner=("Dockerfile", ".dockerignore", "run-tests.sh", "vitest.config.mjs"),
    build_args=runner_build_args,
    part_command=_part_command,
    # Node, from the official image the runner is built on.
    part_runtime=("/usr/local/bin/node",),
)
