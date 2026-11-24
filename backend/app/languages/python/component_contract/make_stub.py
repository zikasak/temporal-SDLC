"""Trusted tool of the routing agent: writes, or extends, the `.pyi` stub of a Python module
with the exact signatures of named symbols, read from the module's source by its syntax
tree (the code is never imported). Run from /workspace/routing:

    python /workspace/tools/make_stub.py <module> <symbol>...

A symbol is a top-level name of the module (`close_room`, `RoomWorkflow`, `NAME`) or a
member of one of its classes (`RoomWorkflow.close`). A class comes with its decorators,
bases and `__init__`; the types and module constants the signatures name come along as
signatures too, and names imported from other code as the same imports. A type another
module of the repository defines is stubbed in that module's stub too (its fields and
signatures, without a docstring), so the stub imports it; only a name the code does not
define anywhere the tool can read stays `Any`. Every named symbol gets
a placeholder docstring, which has to be replaced with the behaviour it states. The tool
only adds statements to a stub; it never changes one, and it refuses to add a member to a
class of the published stub, which may not change.

    python /workspace/tools/make_stub.py --check

checks instead that every statement of the published stubs (repository/contracts/components)
is still in components/ exactly as published, as the host checks the contract: an import
line or a docstring of a published statement is part of it. It exits 1 naming each one
that was changed or removed.

Options: --repository (default repository), --components (default components),
--source-root (default the `source_root` of components/runtime.json, else `.`).
"""
import argparse
import ast
import builtins
import copy
import json
import sys
from pathlib import Path
from typing import TypeGuard, cast, overload

# What a named symbol's docstring says until the agent states the behaviour; the host
# rejects a contract that still holds it.
PLACEHOLDER = 'TODO: state the behaviour the brief or the existing contract asks for.'
PUBLISHED = Path('contracts/components')
_TYPE_FACTORIES = {'TypeVar', 'NewType', 'ParamSpec', 'TypeVarTuple'}
_SIMPLE_VALUES = (ast.Constant, ast.Name, ast.Attribute, ast.Subscript, ast.BinOp,
                  ast.Tuple, ast.List)
_BUILTINS = set(dir(builtins))


class StubError(Exception):
    pass


def _module_path(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split('.'))
    for candidate in (base.with_name(base.name + '.py'), base / '__init__.py'):
        if candidate.is_file():
            return candidate
    return None


def _stub_path(components: Path, module: str, package: bool) -> Path:
    base = components.joinpath(*module.split('.'))
    return base / '__init__.pyi' if package else base.with_name(base.name + '.pyi')


def _bound(statement: ast.stmt) -> list[str]:
    """The top-level names a statement binds."""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [statement.name]
    if isinstance(statement, ast.Assign):
        return [target.id for target in statement.targets if isinstance(target, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return [statement.target.id]
    if isinstance(statement, ast.TypeAlias) and isinstance(statement.name, ast.Name):
        return [statement.name.id]
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split('.')[0] for alias in statement.names]
    return []


def _public_property(statement: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return not statement.name.startswith('_') and any(
        isinstance(item, ast.Name) and item.id in ('property', 'cached_property')
        or isinstance(item, ast.Attribute) and item.attr == 'cached_property'
        for item in statement.decorator_list)


def _type_checking(statement: ast.stmt) -> TypeGuard[ast.If]:
    test = statement.test if isinstance(statement, ast.If) else None
    return (isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING'
            or isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')


class Source:
    """A module of the repository as its syntax tree has it."""

    def __init__(self, root: Path, module: str):
        path = _module_path(root, module)
        if path is None:
            raise StubError(f'No source of module {module} under {root}')
        self.root, self.module, self.path = root, module, path
        self.package = path.name == '__init__.py'
        self.tree = ast.parse(path.read_text(encoding='utf-8'))
        self.definitions: dict[str, list[ast.stmt]] = {}
        self.imports: dict[str, tuple[ast.stmt, ast.alias]] = {}
        statements = list(self.tree.body)
        for statement in self.tree.body:
            if _type_checking(statement):
                statements += statement.body
        for statement in statements:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                for alias in statement.names:
                    if alias.name != '*':
                        self.imports[alias.asname or alias.name.split('.')[0]] = (
                            statement, alias)
            else:
                for name in _bound(statement):
                    self.definitions.setdefault(name, []).append(statement)

    def absolute(self, statement: ast.ImportFrom) -> str:
        if not statement.level:
            return statement.module or ''
        package = self.module.split('.') if self.package else self.module.split('.')[:-1]
        base = package[:len(package) - statement.level + 1]
        return '.'.join([*base, *([statement.module] if statement.module else [])])


class Stubber:
    def __init__(self, source: Source, components: Path, run: 'Run'):
        self.source, self.components, self.run = source, components, run
        # Top-level name -> members wanted of it (None: the symbol itself only).
        self.wanted: dict[str, set[str] | None] = {}
        self.named: set[str] = set()
        self.imports: dict[str, str] = {}
        self.unresolved: set[str] = set()

    # -- what a name refers to ------------------------------------------------------

    def _repository_module(self, module: str) -> bool:
        return _module_path(self.source.root, module) is not None

    def _stub_names(self, module: str) -> set[str] | None:
        for package in (False, True):
            path = _stub_path(self.components, module, package)
            if path.is_file():
                try:
                    tree = ast.parse(path.read_text(encoding='utf-8'))
                except SyntaxError:
                    return None
                return {name for statement in tree.body for name in _bound(statement)}
        return None

    def resolve(self, name: str) -> bool:
        """Make `name` available to the stub; false when only `Any` can stand for it."""
        if name in self.source.definitions:
            self.wanted.setdefault(name, None)
            return True
        if name in self.imports:
            return True
        if name in self.source.imports:
            statement, alias = self.source.imports[name]
            if isinstance(statement, ast.ImportFrom):
                module = self.source.absolute(statement)
                if module == '__future__':
                    return False
                if self._repository_module(module):
                    names = self._stub_names(module)
                    if ((names is None or alias.name not in names)
                            and not self.follow(module, alias.name)):
                        self.unresolved.add(name)
                        return False
                text = f'from {module} import {alias.name}' + (
                    f' as {alias.asname}' if alias.asname else '')
            else:
                if self._repository_module(alias.name) and self._stub_names(alias.name) is None:
                    self.unresolved.add(name)
                    return False
                text = f'import {alias.name}' + (f' as {alias.asname}' if alias.asname else '')
            self.imports[name] = text
            return True
        if name in _BUILTINS:
            return True
        self.unresolved.add(name)
        return False

    def resolve_attribute(self, chain: list[str]) -> bool:
        """Make `a.b.T` available, where `a` names a module of the repository (`import
        app.store`, `import app.store as a`, `from app import store`): the type is stubbed
        in its module's stub, which the stub imports as the code does."""
        root = chain[0]
        if root not in self.source.imports or len(chain) < 2:
            return self.resolve(root)
        statement, alias = self.source.imports[root]
        if isinstance(statement, ast.ImportFrom):
            parent = self.source.absolute(statement)
            if parent == '__future__':
                return False
            # `from app import store` (as a): a.T is app.store.T.
            module = '.'.join(filter(None, [parent, alias.name, *chain[1:-1]]))
            text = f'from {parent} import {alias.name}'
        elif alias.asname:
            # `import app.store as a`: a.T is app.store.T.
            module = '.'.join([alias.name, *chain[1:-1]])
            text = f'import {alias.name}'
        else:
            # `import app.store`: app.store.T.
            module = '.'.join(chain[:-1])
            text = f'import {alias.name}'
        text += f' as {alias.asname}' if alias.asname else ''
        if not self._repository_module(module):
            return self.resolve(root)
        names = self._stub_names(module)
        if (names is None or chain[-1] not in names) and not self.follow(module, chain[-1]):
            self.unresolved.add('.'.join(chain))
            return False
        self.imports.setdefault(root, text)
        return True

    def follow(self, module: str, symbol: str) -> bool:
        """Stub `symbol`, which another module of the repository defines, in that module's
        stub, so this stub can import it rather than stand `Any` for it."""
        active = self.run.active.get(module)
        if active is not None:
            # A module whose stub this run is writing: it gains the symbol too.
            return active.resolve(symbol)
        try:
            self.run.stub(module, [symbol], named=False)
        except (StubError, SyntaxError, OSError, UnicodeDecodeError):
            return False
        names = self._stub_names(module)
        return names is not None and symbol in names

    @overload
    def types(self, node: ast.expr) -> ast.expr: ...

    @overload
    def types(self, node: None) -> None: ...

    @overload
    def types(self, node: ast.expr | None) -> ast.expr | None: ...

    def types(self, node: ast.expr | None) -> ast.expr | None:
        """A type expression with every name made available, or `Any` in its place."""
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode='eval').body
            except SyntaxError:
                return self._any()
        stubber = self

        class Names(ast.NodeTransformer):
            def visit_Name(self, item: ast.Name) -> ast.expr:
                return item if stubber.resolve(item.id) else stubber._any()

            def visit_Attribute(self, item: ast.Attribute) -> ast.expr:
                root: ast.expr = item
                chain: list[str] = []
                while isinstance(root, ast.Attribute):
                    chain.insert(0, root.attr)
                    root = root.value
                if isinstance(root, ast.Name):
                    return (item if stubber.resolve_attribute([root.id, *chain])
                            else stubber._any())
                return cast(ast.expr, self.generic_visit(item))

            def visit_Subscript(self, item: ast.Subscript) -> ast.expr:
                head = item.value
                name = head.id if isinstance(head, ast.Name) else getattr(head, 'attr', '')
                if name == 'Literal':
                    # Its strings are values, not forward references.
                    item.value = self.visit(head)
                    return item
                return cast(ast.expr, self.generic_visit(item))

            def visit_Constant(self, item: ast.Constant) -> ast.expr:
                if isinstance(item.value, str) and item is not node:
                    # A forward reference inside a type, such as list['Room'].
                    try:
                        inner = ast.parse(item.value, mode='eval').body
                    except SyntaxError:
                        return item
                    return stubber.types(inner)
                return item

        return cast(ast.expr, Names().visit(copy.deepcopy(node)))

    def _any(self) -> ast.Name:
        self.imports.setdefault('Any', 'from typing import Any')
        return ast.Name('Any')

    def _decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
                    ) -> list[ast.expr]:
        """Decorators as they are; their module-level names are made available, others
        (such as `@value.setter`) are names of the class."""
        for decorator in node.decorator_list:
            for item in ast.walk(decorator):
                root = item
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and (root.id in self.source.imports
                                                   or root.id in self.source.definitions):
                    self.resolve(root.id)
        return copy.deepcopy(node.decorator_list)

    # -- statements -----------------------------------------------------------------

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                 documented: bool) -> ast.stmt:
        stub = copy.deepcopy(node)
        arguments = stub.args
        for item in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                     *filter(None, [arguments.vararg, arguments.kwarg])]:
            item.annotation = self.types(item.annotation)
            item.type_comment = None
        arguments.defaults = [item if isinstance(item, ast.Constant) else
                              ast.Constant(Ellipsis) for item in arguments.defaults]
        arguments.kw_defaults = [item if item is None or isinstance(item, ast.Constant) else
                                 ast.Constant(Ellipsis) for item in arguments.kw_defaults]
        stub.returns = self.types(stub.returns)
        stub.decorator_list = self._decorators(node)
        stub.type_comment = None
        body: list[ast.stmt] = [ast.Expr(ast.Constant(PLACEHOLDER))] if documented else []
        stub.body = [*body, ast.Expr(ast.Constant(Ellipsis))]
        return stub

    def assignment(self, node: ast.stmt) -> ast.stmt:
        if isinstance(node, ast.AnnAssign):
            value = node.value if isinstance(node.value, ast.Constant) else None
            return ast.AnnAssign(copy.deepcopy(node.target), self.types(node.annotation),
                                 copy.deepcopy(value), simple=1)
        if isinstance(node, ast.TypeAlias):
            return ast.TypeAlias(copy.deepcopy(node.name), copy.deepcopy(node.type_params),
                                 self.types(node.value))
        assert isinstance(node, ast.Assign)  # the only other assignment a stub keeps
        value = node.value
        factory = (isinstance(value, ast.Call) and (
            getattr(value.func, 'id', None) or getattr(value.func, 'attr', None))
            in _TYPE_FACTORIES)
        if isinstance(value, _SIMPLE_VALUES) or factory:
            return ast.Assign(copy.deepcopy(node.targets), self.types(value), lineno=0)
        target = cast(ast.Name | ast.Attribute | ast.Subscript, copy.deepcopy(node.targets[0]))
        return ast.AnnAssign(target, self._any(), None, simple=1)

    def member(self, statement: ast.stmt, members: set[str],
               properties: bool = False) -> list[ast.stmt]:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (statement.name == '__init__' or statement.name in members
                    or properties and _public_property(statement)):
                return [self.function(statement, statement.name in members)]
        elif isinstance(statement, ast.ClassDef):
            if statement.name in members:
                return [self.klass(statement, set(), True)]
        elif isinstance(statement, ast.AnnAssign):
            # Declared fields, which a dataclass or a model builds its __init__ from.
            return [self.assignment(statement)]
        elif isinstance(statement, ast.Assign):
            if set(_bound(statement)) & members:
                return [self.assignment(statement)]
        return []

    def klass(self, node: ast.ClassDef, members: set[str], documented: bool) -> ast.ClassDef:
        missing = members - {name for statement in node.body for name in _bound(statement)}
        if missing:
            raise StubError(f'Class {node.name} of {self.source.module} has no member '
                            f'{", ".join(sorted(missing))}; hand-write a member the code '
                            'does not have yet')
        body: list[ast.stmt] = (
            [ast.Expr(ast.Constant(PLACEHOLDER))] if documented else [])
        # A type no member is named of, such as one a signature names, comes with its
        # public properties: what a test reads of a value it builds.
        for statement in node.body:
            body += self.member(statement, members, properties=not members)
        return ast.ClassDef(
            name=node.name, bases=[self.types(base) for base in node.bases],
            keywords=copy.deepcopy(node.keywords), decorator_list=self._decorators(node),
            body=body or [ast.Expr(ast.Constant(Ellipsis))],
            type_params=copy.deepcopy(getattr(node, 'type_params', [])))

    def top(self, name: str, members: set[str] | None) -> list[ast.stmt]:
        statements = []
        documented = name in self.named
        for node in self.source.definitions[name]:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                statements.append(self.function(node, documented))
            elif isinstance(node, ast.ClassDef):
                statements.append(self.klass(node, members or set(), documented))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
                statements.append(self.assignment(node))
        return statements


def _published_names(published: Path) -> set[str]:
    if not published.is_file():
        return set()
    return {name for statement in ast.parse(published.read_text(encoding='utf-8')).body
            for name in _bound(statement)}


def _statements(source: str) -> list[str]:
    """A stub's top-level statements as the host compares them (`statements` of
    app.languages.python.components)."""
    return [ast.unparse(statement) for statement in ast.parse(source).body]


def check_published(repository: Path, components: Path) -> list[str]:
    """What the stubs in `components` changed or removed of the published ones."""
    root = repository / PUBLISHED
    problems: list[str] = []
    for file in sorted(root.rglob('*.pyi')) if root.is_dir() else []:
        relative = file.relative_to(root).as_posix()
        stub = components / relative
        if not stub.is_file():
            problems.append(f'{relative}: the published stub was removed')
            continue
        try:
            kept = set(_statements(stub.read_text(encoding='utf-8')))
        except SyntaxError as exc:
            problems.append(f'{relative}: not valid Python: {exc}')
            continue
        problems += [f'{relative}: the published statement `{statement.splitlines()[0]}` '
                     'was changed or removed'
                     for statement in _statements(file.read_text(encoding='utf-8'))
                     if statement not in kept]
    return problems


class Run:
    """One run of the tool: the stubs it writes, the named module's and those of the
    modules whose types its signatures name."""

    def __init__(self, repository: Path, components: Path, source_root: str):
        self.repository, self.components = repository, components
        self.root = repository / source_root
        # The modules whose stub is being written, with what writes it.
        self.active: dict[str, Stubber] = {}
        # `module.Name` of each type stubbed because a signature names it.
        self.followed: list[str] = []
        self.unresolved: set[str] = set()

    def stub(self, module: str, symbols: list[str], named: bool = True) -> Path:
        """Write or extend the module's stub with the symbols; returns the stub's path.
        Symbols not `named` are types another stub names: they get no docstring, and one
        the module only imports is stubbed as that import."""
        source = Source(self.root, module)
        stubber = Stubber(source, self.components, self)
        for symbol in symbols:
            name, _, member = symbol.partition('.')
            if name not in source.definitions:
                if not named and name in source.imports:
                    if not stubber.resolve(name):
                        raise StubError(f'{module}.{name} cannot be stubbed')
                    continue
                raise StubError(f'{module} has no top-level {name}; hand-write a symbol the '
                                'code does not have yet')
            if member and not isinstance(source.definitions[name][-1], ast.ClassDef):
                raise StubError(f'{module}.{name} is not a class')
            wanted = stubber.wanted.get(name) or set()
            stubber.wanted[name] = wanted | ({member} if member else set())
            if named:
                stubber.named.add(member and f'{name}.{member}' or name)
        stub = next((path for path in (_stub_path(self.components, module, package)
                                       for package in (source.package, not source.package))
                     if path.is_file()), _stub_path(self.components, module, source.package))
        existing = stub.read_text(encoding='utf-8') if stub.is_file() else ''
        present = {name for statement in ast.parse(existing).body
                   for name in _bound(statement)}
        published = _published_names(
            self.repository / PUBLISHED / stub.relative_to(self.components))
        # Members named for a class are documented as the class itself is not.
        for name, members in list(stubber.wanted.items()):
            if members and named:
                stubber.named.discard(name)
                stubber.named.update(f'{name}.{member}' for member in members)
        statements: list[ast.stmt] = []
        done: set[str] = set()
        text = existing
        self.active[module] = stubber
        try:
            # Stubbing a symbol can want more of this module, also from a stub of another
            # module that names one of its types.
            while len(done) < len(stubber.wanted):
                for name, members in list(stubber.wanted.items()):
                    if name in done:
                        continue
                    done.add(name)
                    if name in present:
                        if members:
                            text = _extend_class(text, name, members, stubber, published,
                                                 module)
                        continue
                    statements += stubber.top(name, members)
        finally:
            del self.active[module]
        imports = [line for name, line in sorted(stubber.imports.items())
                   if name not in present and name not in stubber.wanted]
        added = '\n'.join([*imports, *(ast.unparse(ast.fix_missing_locations(item))
                                       for item in statements)])
        if added:
            text = (text.rstrip('\n') + '\n\n' if text.strip() else '') + added + '\n'
        ast.parse(text)
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(text, encoding='utf-8')
        if not named:
            self.followed += [f'{module}.{name}' for name in symbols]
        self.unresolved |= stubber.unresolved
        return stub


def make_stub(module: str, symbols: list[str], repository: Path, components: Path,
              source_root: str) -> Path:
    """Write or extend the module's stub with the symbols, and the stubs of the types
    their signatures name; returns the stub's path."""
    run = Run(repository, components, source_root)
    stub = run.stub(module, symbols)
    if run.followed:
        print(f'Also stubbed the types the signatures name, in their own modules\' stubs: '
              f'{", ".join(run.followed)}')
    if run.unresolved:
        print(f'Stubbed as Any (the code defines them nowhere make_stub can read): '
              f'{", ".join(sorted(run.unresolved))}; the host rejects a type of the code '
              'stubbed as Any, so hand-write those types in the stub of their module',
              file=sys.stderr)
    return stub


def _extend_class(text: str, name: str, members: set[str], stubber: Stubber,
                  published: set[str], module: str) -> str:
    """Add members to a class the stub already has, after its last line."""
    tree = ast.parse(text)
    node = next(item for item in tree.body
                if isinstance(item, ast.ClassDef) and item.name == name)
    have = {member for statement in node.body for member in _bound(statement)}
    missing = members - have
    if not missing:
        return text
    if name in published:
        raise StubError(f'Class {name} of the published stub of {module} lacks '
                        f'{", ".join(sorted(missing))}; a published statement may not change')
    source = next(item for item in stubber.source.definitions[name]
                  if isinstance(item, ast.ClassDef))
    new: list[ast.stmt] = []
    for statement in source.body:
        if set(_bound(statement)) & missing:
            new += stubber.member(statement, missing)
    indent = ' ' * (node.body[0].col_offset if node.body else 4)
    lines = text.splitlines()
    end = node.end_lineno or node.lineno
    body = [indent + line if line else line
            for item in new for line in ast.unparse(ast.fix_missing_locations(item)).splitlines()]
    return '\n'.join([*lines[:end], *body, *lines[end:]]) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0])
    parser.add_argument('module', nargs='?')
    parser.add_argument('symbols', nargs='*')
    parser.add_argument('--check', action='store_true',
                        help='check the published stubs are kept, and write nothing')
    parser.add_argument('--repository', default='repository', type=Path)
    parser.add_argument('--components', default='components', type=Path)
    parser.add_argument('--source-root')
    arguments = parser.parse_args()
    if arguments.check:
        problems = check_published(arguments.repository, arguments.components)
        for problem in problems:
            print(f'make_stub: {problem}', file=sys.stderr)
        if problems:
            print('make_stub: restore each published statement exactly as it was. Add an '
                  'import as a statement of its own (`from typing import Final`), never by '
                  'editing a published one, and state new behaviour in the docstrings of the '
                  'symbols you add, not in a published docstring.', file=sys.stderr)
            return 1
        print('The published stubs are kept.')
        return 0
    if not arguments.module or not arguments.symbols:
        parser.error('a module and at least one symbol are required, or --check')
    source_root = arguments.source_root
    if source_root is None:
        try:
            source_root = json.loads(
                (arguments.components / 'runtime.json').read_text())['source_root']
        except (OSError, ValueError, KeyError, TypeError):
            source_root = '.'
    try:
        stub = make_stub(arguments.module, arguments.symbols, arguments.repository,
                         arguments.components, source_root)
    except (StubError, SyntaxError) as exc:
        print(f'make_stub: {exc}', file=sys.stderr)
        return 1
    print(f'Wrote {stub}. Replace each placeholder docstring ("{PLACEHOLDER}") with the '
          'behaviour the symbol has to have, with edit_file.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
