"""Component contracts of Java services: one stub per stubbed type, a Java source whose
methods and constructors end in `;` instead of a body, checked against the compiled code
by the trusted Stubs.java (javac's own reading of both)."""

import re
from pathlib import Path, PurePosixPath

from app.languages import RUNTIME_FILE, TOOLS_DIR, ComponentContract

RUNNER = Path(__file__).parent / "component_contract"
# The trusted tool the routing agent and the component test author check their work with,
# as the conformance check does.
STUBS_TOOL = RUNNER / "Stubs.java"
# Where the agents find it.
TOOLS = f"/workspace/{TOOLS_DIR}"
# The version of maven-dependency-plugin every runner and agent lists class paths with.
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"
PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,200}")

INSTRUCTIONS = """Component contract (Java). It lives in /workspace/routing/components/,
which already holds the repository's published contract (its contracts/components/), if
there is one. Keep every published file: you may add stubs and add declarations to a
published stub, but never change or remove one of its declarations.
- {runtime}: {{"project": "…"}}, the directory of the repository, relative to it, of the
  Maven project (its pom.xml) that compiles the stubbed classes (`.` for the repository
  itself).
- One stub per stubbed top-level type, at the path of its source below components/
  (com/example/flows/FlowWorkflow.java for com.example.flows.FlowWorkflow), with its
  package and the imports it needs. A seam's `module` is the type's fully qualified name,
  and its symbols are named from the type's simple name: `FlowWorkflow`,
  `FlowWorkflow.close` (all overloads of a method share its name), `FlowWorkflow.Inner`
  and `FlowWorkflow.Inner.method`; a constructor is named like its type
  (`FlowWorkflowImpl.FlowWorkflowImpl`).
- A stub holds only the seam's members and the types and collaborators they mention,
  each as it already exists in the code: the type's declaration with its modifiers,
  annotations, type parameters, superclass and the interfaces a test relies on; methods
  and constructors with their exact modifiers, annotations kept in class files (such as
  Temporal's @WorkflowInterface, @SignalMethod or @ActivityMethod(name = …)), type
  parameters, parameter types, return type and throws clause; fields with their type and
  modifiers; enum constants; and a Javadoc comment that states the behaviour the brief or
  the existing contract asks for, never how the code achieves it. Every method and
  constructor ends in `;` instead of a body, whatever its modifiers; no initializer block,
  no enum constant body, no lambda, no `new`, and a field's value, if any, only a constant.
  Collaborators the seam calls (activities, repositories, clients) appear as signatures
  only, so a test author can fake them.
- The stubs must compile as Java once bodies are added, against the project's
  dependencies alone: stub every type of the code a stub names (its declaration may be
  all a stub of it holds), and in a stub of a concrete class declare every abstract
  method it implements.
- Check the contract before you finish, from /workspace/routing/repository/<project>:
  mvn -B -q -Dmaven.test.skip=true compile {plugin}:build-classpath
  -Dmdep.outputFile=/tmp/dependencies.txt -DincludeScope=compile, then
  java {tools}/Stubs.java check /workspace/routing/components target/classes
  /tmp/dependencies.txt <every stubbed type's fully qualified name>. It must report no
  error but, for each symbol a seam lists in `added` or `added_elsewhere`, that it is not
  present at runtime."""

# Characters a stub may hold outside comments, strings and character literals.
TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<doc>/\*\*(?!/).*?\*/)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<text>\"\"\"[ \t\f]*\n(?:\\.|(?!\"\"\").)*?\"\"\")
  | (?P<string>"(?:\\.|[^"\\\n])*")
  | (?P<char>'(?:\\.|[^'\\\n])+')
  | (?P<number>\.?\d(?:[eEpP][+-]|[\w.])*)
  | (?P<name>(?:[^\W\d]|\$)[\w$]*)
  | (?P<op>->|::|\.\.\.|[^\s\w])
""", re.S | re.X)
MODIFIERS = {"public", "protected", "private", "static", "abstract", "final", "sealed",
             "non-sealed", "strictfp", "default", "synchronized", "native", "transient",
             "volatile"}
TYPE_KINDS = {"class", "interface", "enum", "@interface", "record"}
# Tokens of code: none of them belongs in a declaration.
CODE = {"->", "::", "new", "switch", "return", "throw", "if", "for", "while", "do", "try",
        "assert"}
OPENING = {"(": ")", "[": "]", "{": "}"}


class _Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind: str, value: str, line: int) -> None:
        self.kind, self.value, self.line = kind, value, line


def _tokens(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    position, line = 0, 1
    while position < len(source):
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
    # `non-sealed` and `@interface` are one word each.
    merged: list[_Token] = []
    for token in tokens:
        if (token.value == "sealed" and len(merged) >= 2 and merged[-1].value == "-"
                and merged[-2].value == "non"):
            merged[-2:] = [_Token("name", "non-sealed", token.line)]
        elif token.value == "interface" and merged and merged[-1].value == "@":
            merged[-1] = _Token("name", "@interface", token.line)
        else:
            merged.append(token)
    return merged


def _called(words: list[str]) -> bool:
    """Whether a `(` after these words would call a method."""
    return bool(words) and re.fullmatch(r"[^\W\d][\w$]*", words[-1]) is not None


def _doc(text: str) -> str:
    lines = [re.sub(r"^\s*\*\s?", "", line) for line in text[3:-2].splitlines()]
    return " ".join(" ".join(lines).split())


class _Stub:
    """Reads a stub's declarations, refusing anything else, and collects its symbols and
    statements: one per declaration, prefixed with the types it is declared in."""

    def __init__(self, source: str) -> None:
        self.tokens = _tokens(source)
        self.index = 0
        self.symbols: set[str] = set()
        self.statements: list[str] = []
        self.types: list[str] = []
        self.package = ""

    def peek(self, offset: int = 0) -> str | None:
        index = self.index + offset
        return self.tokens[index].value if index < len(self.tokens) else None

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
        if token.kind != "name" or token.value in MODIFIERS or token.value in CODE:
            raise ValueError(f"line {line}: expected a name, not {token.value!r}")
        return token.value

    def docs(self) -> str:
        doc = ""
        while self.index < len(self.tokens) and self.tokens[self.index].kind == "doc":
            doc = _doc(self.take().value)
        return doc

    def balanced(self, words: list[str]) -> None:
        """A bracketed group of constants, names, types and annotations, such as a
        method's parameters or an annotation's arguments: no code."""
        closing = [OPENING[self.peek()]]
        words.append(self.take().value)
        while closing:
            line, value = self.line(), self.peek()
            if value is None:
                raise ValueError(f"line {line}: unbalanced {closing[-1]!r}")
            if self.tokens[self.index].kind == "doc":
                self.take()
                continue
            if value in CODE:
                raise ValueError(f"line {line}: a stub holds no code ({value})")
            if value == "@" and self.peek(1) != "interface":
                self.annotation(words)
                continue
            if value in OPENING:
                if value == "(" and _called(words):
                    raise ValueError(f"line {line}: a stub holds no code (a call of "
                                     f"{words[-1]})")
                closing.append(OPENING[value])
            elif value in ")]}":
                if value != closing.pop():
                    raise ValueError(f"line {line}: unbalanced {value!r}")
            words.append(self.take().value)

    def annotation(self, words: list[str]) -> None:
        words.append(self.take().value)  # @
        words.append(self.name())
        while self.peek() == "." and self.peek(1) not in (None, "class"):
            words.append(self.take().value)
            words.append(self.name())
        if self.peek() == "(":
            self.balanced(words)

    def modifiers(self, words: list[str]) -> None:
        while True:
            value = self.peek()
            if value == "@" and self.peek(1) != "interface":
                self.annotation(words)
            elif value in MODIFIERS:
                words.append(self.take().value)
            elif self.index < len(self.tokens) and self.tokens[self.index].kind == "doc":
                self.take()
            else:
                return

    def record(self, scope: str, doc: str, words: list[str]) -> None:
        self.statements.append(f"{scope}|{doc}|{' '.join(words)}")

    # ---- compilation unit ----

    def unit(self) -> None:
        self.docs()
        if self.peek() == "package":
            words = [self.take().value]
            while self.peek() != ";":
                words.append(self.take().value)
            self.take()
            self.package = "".join(words[1:])
            self.record("", "", words)
        while self.peek() == "import":
            words = [self.take().value]
            while self.peek() != ";":
                line, token = self.line(), self.take()
                if not (token.kind == "name" or token.value in (".", "*")):
                    raise ValueError(f"line {line}: an import names a type or a member")
                words.append(token.value)
            self.take()
            self.record("", "", words)
            self.docs()
        while self.peek() == ";":
            self.take()
        if self.index >= len(self.tokens):
            raise ValueError("the stub declares no type")
        self.type_declaration("", self.docs())
        while self.peek() == ";" or (self.index < len(self.tokens)
                                     and self.tokens[self.index].kind == "doc"):
            self.take()
        if self.index < len(self.tokens):
            raise ValueError(f"line {self.line()}: a stub declares one top-level type")

    def is_type(self) -> bool:
        value = self.peek()
        if value == "record":
            # A contextual keyword: `record Name(` or `record Name<`.
            return (self.index + 1 < len(self.tokens)
                    and self.tokens[self.index + 1].kind == "name"
                    and self.peek(2) in ("(", "<"))
        return value in TYPE_KINDS

    def type_declaration(self, scope: str, doc: str) -> None:
        words: list[str] = []
        self.modifiers(words)
        line, kind = self.line(), self.take().value
        if kind not in TYPE_KINDS:
            raise ValueError(f"line {line}: expected a type declaration, not {kind!r}")
        name = self.name()
        words += [kind, name]
        # Type parameters, a record's header, extends, implements and permits.
        while self.peek() != "{":
            value = self.peek()
            if value is None:
                raise ValueError(f"line {line}: the declaration of {name} has no body")
            if value == "@":
                self.annotation(words)
            elif value in OPENING:
                self.balanced(words)
            elif value in CODE:
                raise ValueError(f"line {self.line()}: a stub holds no code ({value})")
            else:
                words.append(self.take().value)
        self.take()
        self.symbols.add(f"{scope}{name}")
        self.record(scope, doc, words)
        if kind == "record":
            self.components(f"{scope}{name}.", words)
        self.body(f"{scope}{name}.", kind)

    def components(self, scope: str, header: list[str]) -> None:
        """A record's components are its accessors."""
        try:
            start = header.index("(")
        except ValueError:
            return
        depth, previous = 0, None
        for word in header[start:]:
            if word in "([{<":
                depth += 1
            elif word in ")]}>":
                if depth == 1 and previous and word == ")":
                    self.symbols.add(f"{scope}{previous}")
                depth -= 1
            elif word == "," and depth == 1 and previous:
                self.symbols.add(f"{scope}{previous}")
            if depth == 0:
                break
            previous = word if re.fullmatch(r"[^\W\d][\w$]*", word) else None

    # ---- members ----

    def body(self, scope: str, kind: str) -> None:
        if kind == "enum":
            self.constants(scope)
        while True:
            doc = self.docs()
            value = self.peek()
            if value is None:
                raise ValueError(f"the body of {scope[:-1]} is not closed")
            if value == "}":
                self.take()
                return
            if value == ";":
                self.take()
                continue
            start = self.index
            words: list[str] = []
            self.modifiers(words)
            if self.peek() == "{":
                raise ValueError(f"line {self.line()}: a stub holds no initializer block")
            if self.is_type():
                self.index = start
                self.type_declaration(scope, doc)
                continue
            self.member(scope, doc, words, kind)

    def constants(self, scope: str) -> None:
        while True:
            doc = self.docs()
            if self.peek() in (";", "}"):
                if self.peek() == ";":
                    self.take()
                return
            words: list[str] = []
            while self.peek() == "@":
                self.annotation(words)
            name = self.name()
            words.append(name)
            if self.peek() == "(":
                self.balanced(words)
            if self.peek() == "{":
                raise ValueError(f"line {self.line()}: a stub holds no enum constant body")
            self.symbols.add(f"{scope}{name}")
            self.record(scope, doc, words)
            if self.peek() == ",":
                self.take()

    def member(self, scope: str, doc: str, words: list[str], kind: str) -> None:
        """A method, constructor or field, up to its `;`."""
        angle = 0
        names: list[str] = []
        while True:
            line, value = self.line(), self.peek()
            if value is None:
                raise ValueError(f"line {line}: a declaration is not closed")
            if value in CODE:
                raise ValueError(f"line {line}: a stub holds no code ({value})")
            if value == "@":
                self.annotation(words)
                continue
            if value == "{":
                raise ValueError(f"line {line}: a stub holds no body")
            if value == "<":
                angle += 1
            elif value == ">":
                angle -= 1
            elif value == "(" and angle == 0:
                name = words[-1] if words else ""
                if not re.fullmatch(r"[^\W\d][\w$]*", name):
                    raise ValueError(f"line {line}: a declaration has no name")
                self.balanced(words)
                self.after_parameters(words)
                self.symbols.add(f"{scope}{name}")
                self.record(scope, doc, words)
                return
            elif value in (",", ";", "=", "[") and angle == 0:
                if words and re.fullmatch(r"[^\W\d][\w$]*", words[-1]) and value != "[":
                    names.append(words[-1])
                if value == "=":
                    words.append(self.take().value)
                    self.constant(words)
                    continue
                if value == ";":
                    self.take()
                    if not names:
                        raise ValueError(f"line {line}: a declaration has no name")
                    self.symbols.update(f"{scope}{name}" for name in names)
                    self.record(scope, doc, words + [";"])
                    return
            elif value in OPENING:
                self.balanced(words)
                continue
            words.append(self.take().value)

    def constant(self, words: list[str]) -> None:
        """A field's value, up to the `,` or `;` that ends it: a constant expression."""
        while self.peek() not in (",", ";", None):
            line, value = self.line(), self.peek()
            if value in CODE:
                raise ValueError(f"line {line}: a stub holds no code ({value})")
            if value in OPENING:
                if value == "(" and _called(words):
                    raise ValueError(f"line {line}: a stub holds no code (a call of "
                                     f"{words[-1]})")
                self.balanced(words)
            else:
                words.append(self.take().value)

    def after_parameters(self, words: list[str]) -> None:
        """What may follow a method's parameters: array brackets of an old-style return
        type, a throws clause, an annotation element's default, and the closing `;`."""
        while True:
            line, value = self.line(), self.peek()
            if value == ";":
                self.take()
                words.append(";")
                return
            if value == "{":
                raise ValueError(f"line {line}: the body of {self.method_name(words)} is "
                                 "more than `;`")
            if value is None or value in CODE:
                raise ValueError(f"line {line}: a declaration is not closed with `;`")
            if value == "default":
                words.append(self.take().value)
                if self.peek() in OPENING:
                    self.balanced(words)
                elif self.peek() == "@":
                    self.annotation(words)
                else:
                    self.constant(words)
            elif value == "@":
                self.annotation(words)
            elif value in OPENING:
                self.balanced(words)
            else:
                words.append(self.take().value)

    @staticmethod
    def method_name(words: list[str]) -> str:
        return words[words.index("(") - 1] if "(" in words else "a method"


def _read(source: str) -> _Stub:
    stub = _Stub(source)
    stub.unit()
    return stub


def _module(path: Path) -> str:
    parts = list(PurePosixPath(path.as_posix()).parts)
    if not parts or not parts[-1].endswith(".java"):
        raise ValueError(f"{path.as_posix()} is not the stub of a type")
    parts[-1] = parts[-1].removesuffix(".java")
    if not all(re.fullmatch(r"[^\W\d][\w$]*", part) for part in parts):
        raise ValueError(f"{path.as_posix()} is not the stub of a type")
    return ".".join(parts)


def _read_stub(module: str, source: str) -> set[str]:
    stub = _read(source)
    package, _, name = module.rpartition(".")
    if stub.package != package:
        raise ValueError(f"its package is {stub.package or 'the unnamed package'}, but its "
                         f"path is that of {package or 'the unnamed package'}")
    if name not in stub.symbols:
        raise ValueError(f"it does not declare {name}, the type its file name names")
    return stub.symbols


def _statements(source: str) -> list[str]:
    return _read(source).statements


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
    pom = target / "pom.xml"
    if linked or not target.is_dir() or pom.is_symlink() or not pom.is_file():
        raise ValueError(f"project {value} is not a directory of the repository with a pom.xml")
    return {"project": path.as_posix()}


def runner_build_args(runtime: dict) -> dict[str, str]:
    """How a runner image builds the code the contract describes: the component tests'
    as well as the conformance check's."""
    return {"PROJECT": runtime["project"]}


COMPONENT_CONTRACT = ComponentContract(
    instructions=INSTRUCTIONS.replace("{plugin}", DEPENDENCY_PLUGIN).replace("{tools}", TOOLS),
    stub_suffix=".java", module=_module, read_stub=_read_stub, statements=_statements,
    read_runtime=_read_runtime, conformance=RUNNER, build_args=runner_build_args,
    tools=(STUBS_TOOL,),
)
