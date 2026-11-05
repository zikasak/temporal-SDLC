"""Component contracts of Python services: `.pyi` stubs of existing modules, checked
against the code with mypy's stubtest."""

import ast
import re
from pathlib import Path, PurePosixPath

from app.languages import RUNTIME_FILE, ComponentContract

# The version both the routing agent and the trusted conformance check run.
MYPY = "mypy==1.18.2"
MAX_REQUIREMENT_FILES = 10
PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,200}")

INSTRUCTIONS = """Component contract (Python). It lives in /workspace/routing/components/,
which already holds the repository's published contract (its contracts/components/), if
there is one. Keep every published file: you may add stubs and add statements to a
published stub, but never change or remove one of its statements.
- {runtime}: {{"source_root": "…", "requirements": ["…"]}}. `source_root` is the
  directory of the repository, relative to it, from which the service's modules are
  imported (`.` for the repository itself); `requirements` lists the repository's pip
  requirements files, relative to it, that install everything those modules import.
- One `.pyi` stub per module of the seam, at the module's path below components/
  (`app/delivery/workflows.pyi` for `app.delivery.workflows`). A stub holds only the
  seam's symbols and the types and collaborators they mention, each as it already exists
  in that module: classes, functions and methods with their exact signatures, decorators
  where they matter (such as @property), a stubbed class's __init__ as the code declares
  it (stubtest compares it even when you leave it out), types the stub cannot import
  from another stub as `Any`, and a docstring that states the behaviour the
  brief or the existing contract asks for, never how the code achieves it. Every function
  body is only a docstring and `...`; no other code, no lambda, no comprehension, no `if`.
  Collaborators the seam calls (activities, repositories, clients) appear as signatures
  only, so a test author can fake them.
- Check the contract before you finish, from /workspace/routing:
  pip install --user {mypy} and the requirements files, then
  PYTHONPATH=repository/<source_root> MYPYPATH=components python -m mypy.stubtest
  --ignore-missing-stub <every stubbed module>. It must report no error but, for each
  symbol a seam lists in `added` or `added_elsewhere`, that it is not present at
  runtime."""


def _module(path: Path) -> str:
    parts = list(PurePosixPath(path.as_posix()).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        raise ValueError(f"{path.as_posix()} is not the stub of a module")
    return ".".join(parts)


# Statements a stub's module or class body may hold. Nothing in a stub runs, but a body
# beyond a signature would hand the test author the implementation.
_DECLARATIONS = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef,
                 ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.TypeAlias, ast.Pass)
# Expressions that carry code of their own.
_CODE = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
         ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)


def _constant(statement: ast.stmt, kind: type) -> bool:
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, kind))


def _declarations(body: list[ast.stmt], scope: str, symbols: set[str]) -> None:
    for statement in body:
        if _constant(statement, str) or _constant(statement, type(Ellipsis)):
            continue
        if not isinstance(statement, _DECLARATIONS):
            raise ValueError(f"line {statement.lineno}: a stub holds declarations only, "
                             f"not {type(statement).__name__}")
        names: list[str] = []
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rest = statement.body[1:] if _constant(statement.body[0], str) else statement.body
            if not (rest == [] or len(rest) == 1 and _constant(rest[0], type(Ellipsis))):
                raise ValueError(f"line {statement.lineno}: the body of {statement.name} "
                                 "is more than a docstring and `...`")
            names = [statement.name]
        elif isinstance(statement, ast.ClassDef):
            _declarations(statement.body, f"{scope}{statement.name}.", symbols)
            names = [statement.name]
        elif isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names = [statement.target.id]
        elif isinstance(statement, ast.TypeAlias) and isinstance(statement.name, ast.Name):
            names = [statement.name.id]
        symbols.update(f"{scope}{name}" for name in names)


def _read_stub(module: str, source: str) -> set[str]:
    try:
        tree = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"not valid Python: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, _CODE):
            raise ValueError(f"line {node.lineno}: a stub holds no code "
                             f"({type(node).__name__})")
    symbols: set[str] = set()
    _declarations(tree.body, "", symbols)
    return symbols


def _statements(source: str) -> list[str]:
    return [ast.dump(statement) for statement in ast.parse(source).body]


def _relative(value: object, repository: Path, what: str, *, directory: bool) -> str:
    if not isinstance(value, str) or not PATH.fullmatch(value):
        raise ValueError(f"{what} is not a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{what} is not a path inside the repository")
    target = repository
    linked = False
    for part in path.parts:
        target = target / part
        linked = linked or target.is_symlink()
    if linked or not (target.is_dir() if directory else target.is_file()):
        raise ValueError(f"{what} {value} is not a {'directory' if directory else 'file'} "
                         "of the repository")
    return path.as_posix()


def _read_runtime(data: object, repository: Path) -> dict:
    if not isinstance(data, dict) or set(data) != {"source_root", "requirements"}:
        raise ValueError(f"{RUNTIME_FILE} holds exactly source_root and requirements")
    requirements = data["requirements"]
    if not isinstance(requirements, list) or len(requirements) > MAX_REQUIREMENT_FILES:
        raise ValueError(f"requirements lists at most {MAX_REQUIREMENT_FILES} files")
    return {
        "source_root": _relative(data["source_root"], repository, "source_root",
                                 directory=True),
        "requirements": [_relative(item, repository, "A requirements file", directory=False)
                         for item in requirements],
    }


def runner_build_args(runtime: dict) -> dict[str, str]:
    """How a runner image installs the code the contract describes: the component tests'
    as well as the conformance check's."""
    return {"SOURCE_ROOT": runtime["source_root"],
            "REQUIREMENTS": " ".join(runtime["requirements"])}


def _build_args(runtime: dict) -> dict[str, str]:
    return {"MYPY": MYPY, **runner_build_args(runtime)}


COMPONENT_CONTRACT = ComponentContract(
    instructions=INSTRUCTIONS.replace("{mypy}", MYPY),
    stub_suffix=".pyi", module=_module, read_stub=_read_stub, statements=_statements,
    read_runtime=_read_runtime, conformance=Path(__file__).parent / "component_contract",
    build_args=_build_args,
)
