"""Component contracts of TypeScript parts of a repository: one `.d.ts` stub per stubbed
module, declarations only, checked against the part's code by the trusted check.mjs
(TypeScript's own compiler, run with the part's tsconfig.json)."""

import json
import re
from pathlib import Path, PurePosixPath

from app.languages import RUNTIME_FILE, TOOLS_DIR, ComponentContract

RUNNER = Path(__file__).parent / "component_contract"
# The trusted tool the routing agent checks its stubs with, as the conformance check does.
CHECK_TOOL = RUNNER / "check.mjs"
TOOLS = f"/workspace/{TOOLS_DIR}"
STUB_SUFFIX = ".d.ts"
# What check.mjs names the files it writes beside a stubbed module: never a module's name.
RESERVED = (".component-stub", ".component-check")
PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,200}")
SEGMENT = re.compile(r"[A-Za-z0-9_$@][A-Za-z0-9_$@.+-]*")
MAX_MANIFEST_BYTES = 1024 * 1024

INSTRUCTIONS = """Component contract (TypeScript) of a part: it lives in
/workspace/routing/parts/<part>/components/, which already holds the part's published
contract (its contracts/parts/<part>/components/), if there is one. Keep every published
file: you may add stubs and add declarations to a published stub, but never change or
remove one of its declarations.
- {runtime}: {{"project": "<part>"}}, the part's directory in the repository (its
  package.json, package-lock.json and tsconfig.json).
- One `.d.ts` stub per module of the seam, at the module's path within the part
  (src/github.d.ts for src/github.ts or src/github.tsx). A seam's `module` is that path
  without its suffix (`src/github`), and its symbols are the module's exports by name
  (`createGitHubApp`), members of an exported class or interface as `Class.member`, a
  constructor as `Class.constructor`.
- A stub holds only `import type` statements and declarations: `export declare function`,
  `export declare const`, `export declare class` (members as signatures only), `export
  interface`, `export type` and `export declare enum`, each describing an export the
  module already has, with the same types, and a JSDoc comment that states the behaviour
  the brief or the existing contract asks for, never how the code achieves it. Types the
  module does not export may be declared in the stub without `export`, as the shapes the
  exports use. No function body, no initializer but a literal constant, no default export
  and no re-export. An exported interface or type alias must describe the whole type;
  an exported class only the members the seam needs. Relative imports resolve from the
  stubbed module's directory, as in the code.
- Check the contract before you finish: copy the part out of the repository
  (cp -r /workspace/routing/repository/<part> /tmp/part), run npm ci --ignore-scripts
  in /tmp/part, then, from /workspace/routing, node {tools}/check.mjs /tmp/part
  parts/<part>/components <every stubbed module>.
  It must report no error but, for each symbol a seam lists in `added`, that it is not
  exported by the code (or is not a member of the code's class)."""

TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<doc>/\*\*(?!/).*?\*/)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<number>\.?\d[\w.]*)
  | (?P<name>(?:[^\W\d]|\$)[\w$]*)
  | (?P<op>=>|\.\.\.|\?\.|[^\s\w])
""", re.S | re.X)
# Words only code holds: none of them belongs in a declaration or a type, except as the
# name of a property (followed by `:` or `?`).
CODE = {"return", "if", "else", "for", "while", "do", "switch", "case", "throw", "try",
        "catch", "finally", "await", "yield", "var", "let", "delete", "debugger", "with",
        "function", "super"}
# Words of a type that something may follow, such as `extends { … }`: not the end of one.
TYPE_WORDS = {"extends", "keyof", "typeof", "infer", "readonly", "unique", "is", "asserts",
              "in", "as", "satisfies", "new", "abstract", "implements", "out", "const"}
MEMBER_MODIFIERS = {"public", "private", "protected", "static", "readonly", "abstract",
                    "declare", "override", "accessor"}
STARTS = {"export", "declare", "import", "interface", "type", "function", "const", "class",
          "enum", "abstract"}
OPENING = {"(": ")", "[": "]", "{": "}", "<": ">"}
LITERAL = re.compile(r"-?(\d[\w.]*|\.\d[\w.]*)|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
                     r"|true|false|null")


class _Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind: str, value: str, line: int) -> None:
        self.kind, self.value, self.line = kind, value, line


def _template(source: str, start: int) -> int:
    """The end of the template literal at `start`, with any `${…}` in it."""
    position = start + 1
    while position < len(source):
        char = source[position]
        if char == "\\":
            position += 2
        elif char == "`":
            return position + 1
        elif source.startswith("${", position):
            position, depth = position + 2, 1
            while depth:
                if position >= len(source):
                    raise ValueError("unterminated template literal")
                char = source[position]
                if char == "`":
                    position = _template(source, position)
                    continue
                if char in "\"'":
                    match = TOKEN.match(source, position)
                    if match is None or match.lastgroup != "string":
                        raise ValueError("unterminated literal")
                    position = match.end()
                    continue
                depth += {"{": 1, "}": -1}.get(char, 0)
                position += 1
        else:
            position += 1
    raise ValueError("unterminated template literal")


def _tokens(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    position, line = 0, 1
    while position < len(source):
        if source[position] == "`":
            try:
                end = _template(source, position)
            except ValueError as exc:
                raise ValueError(f"line {line}: {exc}") from None
            value = source[position:end]
            tokens.append(_Token("template", value, line))
            line += value.count("\n")
            position = end
            continue
        match = TOKEN.match(source, position)
        if match is None:
            raise ValueError(f"line {line}: unreadable character")
        kind, value = match.lastgroup, match.group()
        if kind == "op" and value in "\"'":
            raise ValueError(f"line {line}: unterminated literal")
        if kind == "op" and value == "/" and source.startswith("/*", position):
            raise ValueError(f"line {line}: unterminated comment")
        if kind not in ("space", "comment"):
            tokens.append(_Token(kind, value, line))
        line += value.count("\n")
        position = match.end()
    return tokens


def _doc(text: str) -> str:
    lines = [re.sub(r"^\s*\*\s?", "", line) for line in text[3:-2].splitlines()]
    return " ".join(" ".join(lines).split())


def _ends_operand(token: _Token | None) -> bool:
    """Whether a type can end with this token, so that a `{` after it opens a body."""
    if token is None:
        return False
    if token.kind == "name":
        return token.value not in TYPE_WORDS
    return token.kind in ("string", "number", "template") or token.value in (")", "]", "}", ">")


class _Stub:
    """Reads a stub's declarations, refusing anything else, and collects its exported
    symbols and its statements: one per declaration or member, prefixed with the
    declaration it is in."""

    def __init__(self, source: str) -> None:
        self.tokens = _tokens(source)
        self.index = 0
        self.symbols: set[str] = set()
        self.statements: list[str] = []

    # ---- tokens ----

    def peek(self, offset: int = 0) -> str | None:
        index = self.index + offset
        return self.tokens[index].value if index < len(self.tokens) else None

    def token(self, offset: int = 0) -> _Token | None:
        index = self.index + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def line(self) -> int:
        return self.tokens[min(self.index, len(self.tokens) - 1)].line if self.tokens else 1

    def take(self) -> _Token:
        if self.index >= len(self.tokens):
            raise ValueError("the stub ends in the middle of a declaration")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, value: str) -> None:
        line, token = self.line(), self.take()
        if token.value != value:
            raise ValueError(f"line {line}: expected {value!r}, not {token.value!r}")

    def name(self) -> str:
        line, token = self.line(), self.take()
        if token.kind != "name" or token.value in CODE:
            raise ValueError(f"line {line}: expected a name, not {token.value!r}")
        return token.value

    def docs(self) -> str:
        doc = ""
        while (token := self.token()) is not None and token.kind == "doc":
            doc = _doc(self.take().value)
        return doc

    def record(self, scope: str, doc: str, words: list[str]) -> None:
        self.statements.append(f"{scope}|{doc}|{' '.join(words)}")

    # ---- types ----

    def scan(self, words: list[str], *, comma: bool = False, newline: bool = True,
             stop: tuple[str, ...] = (), defaults: bool = False,
             group: bool = False) -> str | None:
        """A type, or the rest of a signature, up to the `;` (or `,` with `comma`) that
        ends it, which is taken, or the `}` closing the enclosing body or a word of `stop`,
        which is not. A new line after a complete type ends it too, as in TypeScript. No
        code: no keyword only code holds, no assignment, no body; `=` only for the default
        of a type parameter (`defaults` for a group of them). Inside a bracketed `group`, a
        leading `{` is an object type or a parameter's binding pattern, not a body. Returns
        what ended it."""
        closing: list[str] = []
        previous: _Token | None = None
        while True:
            token = self.token()
            if token is None:
                if closing:
                    raise ValueError(f"line {self.line()}: unbalanced {closing[-1]!r}")
                return None
            value = token.value
            if token.kind == "doc":
                if not closing and newline and _ends_operand(previous):
                    # The documentation of what follows a complete type.
                    return "\n"
                self.take()
                continue
            if not closing:
                if value == ";" or (comma and value == ","):
                    self.take()
                    return value
                if value == "}" or value in stop:
                    return value
                if (newline and previous is not None and token.line > previous.line
                        and _ends_operand(previous) and value not in
                        ("|", "&", ".", "=>", "?", ":", "extends", "<", ",", ")", "]", ">")):
                    return "\n"
            if token.kind == "name" and value in CODE and self.peek(1) not in (":", "?") and (
                    previous is None or previous.value != "."):
                raise ValueError(f"line {token.line}: a stub holds no code ({value})")
            if value == "=" and (closing[-1] != ">" if closing else not defaults):
                raise ValueError(f"line {token.line}: a stub holds no initializer or "
                                 "assignment")
            if value == "{" and (_ends_operand(previous)
                                 or previous is None and not words and not group):
                raise ValueError(f"line {token.line}: a stub holds no body")
            if value in OPENING:
                closing.append(OPENING[value])
            elif value in (")", "]", "}", ">") and closing:
                if value != closing[-1]:
                    if value == ">" and closing[-1] != ">":
                        # `=>` is a token of its own; a lone `>` here is a comparison.
                        raise ValueError(f"line {token.line}: a stub holds no code (>)")
                    raise ValueError(f"line {token.line}: unbalanced {value!r}")
                closing.pop()
            elif value in (")", "]", "}", ">"):
                raise ValueError(f"line {token.line}: unbalanced {value!r}")
            words.append(self.take().value)
            previous = token

    def balanced(self, words: list[str]) -> None:
        """A bracketed group: type parameters or a parameter list."""
        opening = self.peek()
        if opening not in OPENING:
            raise ValueError(f"line {self.line()}: expected a bracket, not {opening!r}")
        words.append(self.take().value)
        inner: list[str] = []
        ended = self.scan(inner, newline=False, stop=(OPENING[opening],),
                          defaults=opening == "<", group=True)
        if ended != OPENING[opening]:
            raise ValueError(f"line {self.line()}: unbalanced {opening!r}")
        words += inner
        words.append(self.take().value)

    def end(self, words: list[str], what: str) -> None:
        """The rest of a declaration after its name and type parameters."""
        line = self.line()
        if self.peek() == "{":
            raise ValueError(f"line {line}: {what} has a body; a stub holds signatures only")
        ended = self.scan(words)
        if ended == "}":
            raise ValueError(f"line {self.line()}: unbalanced '}}'")

    # ---- statements ----

    def unit(self) -> None:
        while True:
            doc = self.docs()
            if self.index >= len(self.tokens):
                break
            if self.peek() == ";":
                self.take()
                continue
            if self.peek() == "import":
                self.import_statement()
            else:
                self.declaration(doc)
        if not any("." not in symbol for symbol in self.symbols):
            raise ValueError("the stub exports nothing")

    def import_statement(self) -> None:
        line = self.line()
        words = [self.take().value]
        if self.peek() != "type" or self.peek(1) in ("=", "from"):
            raise ValueError(f"line {line}: a stub imports types only (`import type …`)")
        while self.peek() != "from":
            token = self.token()
            if token is None or not (token.kind == "name" or token.value in "{},*"):
                raise ValueError(f"line {line}: an import names types and the module only")
            words.append(self.take().value)
        words.append(self.take().value)
        token = self.token()
        if token is None or token.kind != "string":
            raise ValueError(f"line {line}: an import names its module as a string")
        words.append(self.take().value)
        if self.peek() == ";":
            self.take()
        self.record("", "", words)

    def declaration(self, doc: str) -> None:
        line = self.line()
        words: list[str] = []
        exported = self.peek() == "export"
        if exported:
            words.append(self.take().value)
            if self.peek() in ("default", "{", "*", "=", "as", "import"):
                raise ValueError(f"line {line}: a stub has no default export and no "
                                 "re-export")
        if self.peek() == "declare":
            words.append(self.take().value)
        kind = self.peek()
        if kind == "abstract" and self.peek(1) == "class":
            words.append(self.take().value)
            kind = "class"
        if kind == "const" and self.peek(1) == "enum":
            words.append(self.take().value)
            kind = "enum"
        if kind == "type" and self.peek(1) == "{":
            raise ValueError(f"line {line}: a stub has no re-export")
        if kind not in ("function", "const", "class", "interface", "type", "enum"):
            raise ValueError(f"line {line}: a stub declares functions, constants, classes, "
                             f"interfaces, types and enums only, not {kind!r}")
        words.append(self.take().value)
        if kind == "const":
            self.constants(doc, words, exported)
            return
        name = self.name()
        words.append(name)
        if self.peek() == "<":
            self.balanced(words)
        if kind == "function":
            if self.peek() != "(":
                raise ValueError(f"line {self.line()}: expected the parameters of {name}")
            self.balanced(words)
            self.end(words, f"function {name}")
        elif kind == "type":
            self.expect("=")
            words.append("=")
            if self.scan(words) == "}":
                raise ValueError(f"line {self.line()}: unbalanced '}}'")
        else:
            self.heritage(words)
        if exported:
            self.symbols.add(name)
        self.record("", doc, words)
        if kind in ("class", "interface"):
            self.body(f"{name}.", exported, kind)
        elif kind == "enum":
            self.enum_members(f"{name}.", exported)

    def constants(self, doc: str, words: list[str], exported: bool) -> None:
        """`const a: T, b = "literal";`: each with a type or a literal value."""
        while True:
            line = self.line()
            name = self.name()
            words.append(name)
            if self.peek() == ":":
                words.append(self.take().value)
                ended = self.scan(words, comma=True, stop=("=",))
                if ended == "=":
                    raise ValueError(f"line {line}: {name} has a type and a value")
            elif self.peek() == "=":
                words.append(self.take().value)
                words.append(self.literal(name))
                ended = self.peek()
                if ended in (";", ","):
                    self.take()
                elif ended is not None and self.token().line == self.tokens[self.index - 1].line:
                    raise ValueError(f"line {line}: the value of {name} is not a literal")
            else:
                raise ValueError(f"line {line}: {name} has neither a type nor a literal value")
            if exported:
                self.symbols.add(name)
            words.append(",")
            if ended != ",":
                break
        self.record("", doc, words[:-1])

    def literal(self, name: str) -> str:
        """A constant's value: a literal, nothing computed."""
        line = self.line()
        value = self.take().value
        if value == "-" and self.token() is not None:
            value += self.take().value
        if not LITERAL.fullmatch(value):
            raise ValueError(f"line {line}: the value of {name} is not a literal")
        return value

    def heritage(self, words: list[str]) -> None:
        """`extends` and `implements` clauses, up to the body's `{`."""
        while self.peek() != "{":
            token = self.token()
            if token is None:
                raise ValueError(f"line {self.line()}: a declaration has no body")
            if token.value in OPENING:
                self.balanced(words)
            elif token.kind == "name" and token.value in CODE or token.value in (";", "="):
                raise ValueError(f"line {token.line}: a stub holds no code ({token.value})")
            else:
                words.append(self.take().value)
        self.take()

    def body(self, scope: str, exported: bool, kind: str) -> None:
        while True:
            doc = self.docs()
            token = self.token()
            if token is None:
                raise ValueError(f"the body of {scope[:-1]} is not closed")
            if token.value == "}":
                self.take()
                return
            if token.value in (";", ","):
                self.take()
                continue
            self.member(scope, doc, exported, kind)

    def member(self, scope: str, doc: str, exported: bool, kind: str) -> None:
        line = self.line()
        words: list[str] = []
        while (self.peek() in MEMBER_MODIFIERS or self.peek() in ("get", "set")) and (
                (following := self.token(1)) is not None
                and (following.kind in ("name", "string") or following.value == "[")):
            if kind == "interface" and self.peek() not in ("readonly", "get", "set"):
                raise ValueError(f"line {line}: an interface member has no {self.peek()}")
            words.append(self.take().value)
        name = None
        token = self.token()
        if token is None:
            raise ValueError(f"line {line}: the body of {scope[:-1]} is not closed")
        if token.kind in ("name", "string", "number"):
            name = token.value.strip("\"'")
            words.append(self.take().value)
        elif token.value == "[":
            # An index signature, or a computed name: no symbol of its own.
            self.balanced(words)
        elif token.value in ("(", "<"):
            pass  # a call signature
        else:
            raise ValueError(f"line {line}: expected a member, not {token.value!r}")
        if name == "new" and self.peek() in ("(", "<"):
            name = None  # a construct signature
        if self.peek() == "?":
            words.append(self.take().value)
        if self.peek() == "<":
            self.balanced(words)
        if self.peek() == "(":
            self.balanced(words)
            if self.peek() == "{":
                raise ValueError(f"line {self.line()}: the body of {name or 'a signature'} "
                                 "is more than a signature")
        if self.peek() == "=":
            raise ValueError(f"line {self.line()}: a stub holds no initializer of a member")
        if self.peek() == "{":
            raise ValueError(f"line {self.line()}: {name or 'a signature'} has a body; a stub "
                             "holds signatures only")
        # The last member may end at the body's `}`.
        self.scan(words)
        if name and exported:
            self.symbols.add(f"{scope}{name}")
        self.record(scope, doc, words)

    def enum_members(self, scope: str, exported: bool) -> None:
        while True:
            doc = self.docs()
            token = self.token()
            if token is None:
                raise ValueError(f"the body of {scope[:-1]} is not closed")
            if token.value == "}":
                self.take()
                return
            if token.value == ",":
                self.take()
                continue
            if token.kind not in ("name", "string"):
                raise ValueError(f"line {token.line}: expected an enum member, not "
                                 f"{token.value!r}")
            name = self.take().value.strip("\"'")
            words = [name]
            if self.peek() == "=":
                words.append(self.take().value)
                words.append(self.literal(name))
            if self.peek() not in (",", "}"):
                raise ValueError(f"line {self.line()}: expected ',' or '}}' after {name}")
            if exported:
                self.symbols.add(f"{scope}{name}")
            self.record(scope, doc, words)


def _read(source: str) -> _Stub:
    stub = _Stub(source)
    stub.unit()
    return stub


def _module(path: Path) -> str:
    posix = path.as_posix()
    if not posix.endswith(STUB_SUFFIX):
        raise ValueError(f"{posix} is not the stub of a module")
    module = posix.removesuffix(STUB_SUFFIX)
    parts = PurePosixPath(module).parts
    if (not parts or not all(SEGMENT.fullmatch(part) and part not in (".", "..")
                             for part in parts)
            or any(marker in parts[-1] for marker in RESERVED)):
        raise ValueError(f"{posix} is not the stub of a module")
    return module


def _read_stub(module: str, source: str) -> set[str]:
    return _read(source).symbols


def _statements(source: str) -> list[str]:
    return _read(source).statements


def _manifest(file: Path) -> dict:
    if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_MANIFEST_BYTES:
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def detect(part: Path) -> bool:
    """A TypeScript project: a package.json that depends on typescript, and a
    tsconfig.json."""
    tsconfig = part / "tsconfig.json"
    if tsconfig.is_symlink() or not tsconfig.is_file():
        return False
    manifest = _manifest(part / "package.json")
    return any(isinstance(manifest.get(key), dict) and "typescript" in manifest[key]
               for key in ("dependencies", "devDependencies"))


def _read_runtime(data: object, repository: Path) -> dict:
    if not isinstance(data, dict) or set(data) != {"project"}:
        raise ValueError(f"{RUNTIME_FILE} holds exactly project")
    value = data["project"]
    if not isinstance(value, str) or not PATH.fullmatch(value):
        raise ValueError("project is not a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("project is not a path inside the repository")
    target, linked = repository, False
    for part in path.parts:
        target = target / part
        linked = linked or target.is_symlink()
    lock = target / "package-lock.json"
    if (linked or not target.is_dir() or not detect(target) or lock.is_symlink()
            or not lock.is_file()):
        raise ValueError(f"project {value} is not a TypeScript project of the repository "
                         "with a package.json, package-lock.json and tsconfig.json")
    return {"project": path.as_posix()}


def runner_build_args(runtime: dict) -> dict[str, str]:
    """How a runner image installs the part the contract describes."""
    return {"PROJECT": runtime["project"]}


COMPONENT_CONTRACT = ComponentContract(
    instructions=INSTRUCTIONS.replace("{tools}", TOOLS),
    stub_suffix=STUB_SUFFIX, module=_module, read_stub=_read_stub, statements=_statements,
    read_runtime=_read_runtime, conformance=RUNNER, build_args=runner_build_args,
    tools=(CHECK_TOOL,),
)
