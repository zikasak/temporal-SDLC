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
line or a docstring of a published statement is part of it. It also follows the code of
each documented function of the stubs that the published stubs do not hold (and every
published symbol named as a seam in `--routes` / `routes.json`), and names the
collaborators it reaches that no stub declares: functions of the repository, classes with
behaviour it builds, and constants computed at import from the environment or the
repository's code (a name the code imports is declared in the stub of its own module, where
a test replaces it). A collaborator the function's docstring (or, for a published seam
symbol, the route's `reason`) names is taken as left to run as it is. It exits 1 naming
each statement that was changed or removed and each collaborator left out. Stubbing a
symbol reports its collaborators the same way, and a symbol the module imports by name is
stubbed as its re-export.

Options: --repository (default repository), --components (default components),
--source-root (default the `source_root` of components/runtime.json, else `.`),
--routes (default routes.json when present, used by --check).
"""
import argparse
import ast
import builtins
import copy
import json
import re
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


# -- collaborators the stubs leave out ------------------------------------------------------

def _stub_module(components: Path, stub: Path) -> str:
    parts = list(stub.relative_to(components).with_suffix('').parts)
    if parts and parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _documented(body: list[ast.stmt], scope: str = '',
                ) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """The functions and methods of a stub that carry a docstring: the seam's symbols, as
    make_stub documents only those (a type a signature names comes without one)."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(statement):
                found.append((scope + statement.name, statement))
        elif isinstance(statement, ast.ClassDef):
            found += _documented(statement.body, f'{scope}{statement.name}.')
    return found


def _qualified(body: list[ast.stmt], scope: str = '') -> set[str]:
    names: set[str] = set()
    for statement in body:
        for name in _bound(statement):
            names.add(scope + name)
        if isinstance(statement, ast.ClassDef):
            names |= _qualified(statement.body, f'{scope}{statement.name}.')
    return names


class Reach:
    """What the code of a stubbed function reaches outside itself that a test could only
    fake if a stub declared it: functions and computed constants of the repository's
    modules, and its classes the code builds. The function's own module's helpers it calls
    are followed rather than named, as are the methods of its own class it calls on
    `self`; anything from outside the repository (the standard library, packages) is
    not a collaborator a stub could declare and is left out."""

    def __init__(self, root: Path, components: Path):
        self.root, self.components = root, components
        self.sources: dict[str, Source | None] = {}

    def source(self, module: str) -> Source | None:
        if module not in self.sources:
            try:
                self.sources[module] = Source(self.root, module)
            except (StubError, SyntaxError, OSError, UnicodeDecodeError):
                self.sources[module] = None
        return self.sources[module]

    def declared(self, module: str) -> set[str]:
        for package in (False, True):
            path = _stub_path(self.components, module, package)
            if path.is_file():
                try:
                    return _qualified(ast.parse(path.read_text(encoding='utf-8')).body)
                except SyntaxError:
                    return set()
        return set()

    def _definition(self, source: Source, qualified: str,
                    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        name, _, member = qualified.partition('.')
        found = source.definitions.get(name, [])
        node = found[-1] if found else None
        if member:
            if not isinstance(node, ast.ClassDef):
                return None
            node = next((item for item in node.body
                         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and item.name == member), None)
        return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None

    def of(self, module: str, qualified: str) -> list[str]:
        """The collaborators `module.qualified` reaches that no stub declares, as
        `module.name`; nothing for a symbol the code does not have yet."""
        source = self.source(module)
        if source is None or self._definition(source, qualified) is None:
            return []
        found: list[str] = []
        self._walk(source, qualified, set(), found)
        return found

    def _walk(self, source: Source, qualified: str, seen: set[str], found: list[str]) -> None:
        if qualified in seen:
            return
        seen.add(qualified)
        node = self._definition(source, qualified)
        if node is None:
            return
        klass = qualified.rpartition('.')[0]
        arguments = node.args
        parameters = [item.arg for item in [*arguments.posonlyargs, *arguments.args,
                                             *arguments.kwonlyargs,
                                             *filter(None, [arguments.vararg,
                                                            arguments.kwarg])]]
        this = parameters[0] if klass and parameters else None
        local = set(parameters)
        skipped: set[int] = set()
        called: set[int] = set()
        for statement in node.body:
            for item in ast.walk(statement):
                if isinstance(item, ast.Name) and not isinstance(item.ctx, ast.Load):
                    local.add(item.id)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    local.add(item.name)
                elif isinstance(item, (ast.Global, ast.Nonlocal)):
                    local.difference_update(item.names)
                elif isinstance(item, ast.ExceptHandler) and item.type is not None:
                    # An exception it catches is a type, not something it calls.
                    skipped.update(id(part) for part in ast.walk(item.type))
                elif isinstance(item, (ast.AnnAssign, ast.arg)) and item.annotation:
                    skipped.update(id(part) for part in ast.walk(item.annotation))
                elif isinstance(item, ast.Call):
                    called.add(id(item.func))
        declared = self.declared(source.module)
        walker = self

        class Visitor(ast.NodeVisitor):
            def visit_Name(self, item: ast.Name) -> None:
                if id(item) not in skipped and item.id not in local:
                    walker._name(source, item.id, id(item) in called, declared, seen,
                                 found)

            def visit_Attribute(self, item: ast.Attribute) -> None:
                chain: list[str] = []
                root: ast.expr = item
                while isinstance(root, ast.Attribute):
                    chain.insert(0, root.attr)
                    root = root.value
                if id(item) in skipped or not isinstance(root, ast.Name):
                    self.generic_visit(item)
                elif this is not None and root.id == this:
                    method = f'{klass}.{chain[0]}'
                    if method not in declared:
                        walker._walk(source, method, seen, found)
                elif root.id in local:
                    pass
                elif not walker._attribute(source, root.id, chain, id(item) in called,
                                           found):
                    if len(chain) == 1 and id(item) in called:
                        walker._name(source, root.id, True, declared, seen, found)
                    else:
                        self.visit(root)

        for statement in node.body:
            Visitor().visit(statement)

    def _add(self, found: list[str], name: str) -> None:
        if name not in found:
            found.append(name)

    def _name(self, source: Source, name: str, called: bool, declared: set[str],
              seen: set[str], found: list[str]) -> None:
        if name in declared:
            return
        definitions = source.definitions.get(name)
        if definitions:
            node = definitions[-1]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and called:
                # A helper of its own module runs as part of it.
                self._walk(source, name, seen, found)
            elif self._collaborator(source, node, called):
                self._add(found, f'{source.module}.{name}')
            return
        if name not in source.imports:
            return
        statement, alias = source.imports[name]
        if not isinstance(statement, ast.ImportFrom):
            return
        module = source.absolute(statement)
        defined = self.source(module)
        if defined is None:
            return
        nodes = defined.definitions.get(alias.name)
        if nodes and self._collaborator(defined, nodes[-1], called):
            # The code looks the name up in its own module, where a test replaces it.
            self._add(found, f'{source.module}.{name}')

    def _attribute(self, source: Source, root: str, chain: list[str], called: bool,
                   found: list[str]) -> bool:
        """Name `root.chain…` when `root` is a module of the repository the code imports
        and the attribute is a collaborator of it; false when `root` is no such module."""
        if root not in source.imports:
            return False
        statement, alias = source.imports[root]
        if isinstance(statement, ast.ImportFrom):
            base = [*filter(None, [source.absolute(statement)]), alias.name]
            rest = chain
        elif alias.asname:
            base, rest = alias.name.split('.'), chain
        else:
            base, rest = [root], chain
        for cut in range(len(rest) - 1, -1, -1):
            module = '.'.join([*base, *rest[:cut]])
            defined = self.source(module)
            if defined is None:
                continue
            name = rest[cut]
            if name in self.declared(module):
                return True
            nodes = defined.definitions.get(name)
            if nodes and self._collaborator(defined, nodes[-1],
                                            called and cut == len(rest) - 1):
                self._add(found, f'{module}.{name}')
            return True
        return False

    def _collaborator(self, source: Source, node: ast.stmt, called: bool) -> bool:
        """Whether a test would need to fake `node` of `source`: a function, a class with
        behaviour of its own that the code builds (not a record, a typed dict or an
        exception), or a constant computed at import from the environment or the
        repository's code (such as a directory read from a variable)."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
        if isinstance(node, ast.ClassDef):
            return called and any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith('__') and not _public_property(item)
                for item in node.body)
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        for item in ast.walk(value) if value is not None else []:
            if (isinstance(item, ast.Name) and item.id in ('getenv', 'environ')
                    or isinstance(item, ast.Attribute) and item.attr in ('getenv', 'environ')):
                return True
            if isinstance(item, ast.Call):
                root = item.func
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and self._repository_name(source, root.id):
                    return True
        return False

    def _repository_name(self, source: Source, name: str) -> bool:
        if name in source.definitions:
            return True
        if name not in source.imports:
            return False
        statement, alias = source.imports[name]
        module = (source.absolute(statement) if isinstance(statement, ast.ImportFrom)
                  else alias.name)
        return _module_path(self.root, module) is not None


def _normalize_module(name: str) -> str:
    """Normalize a path-form module name (e.g. 'app/foo.py') to dotted form."""
    key = name.strip().replace('\\', '/')
    if '/' not in key:
        return key
    while key.startswith('./'):
        key = key[2:]
    key = key.strip('/')
    for suffix in ('.pyi', '.py'):
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[:-len(suffix)]
            break
    key = key.replace('/', '.')
    return key[:-len('.__init__')] if key.endswith('.__init__') else key


def _route_seams(routes_file: Path) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """Service seam symbols and their route reasons from `routes_file`, if readable."""
    if not routes_file.is_file() or routes_file.is_symlink():
        return set(), {}
    try:
        data = json.loads(routes_file.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return set(), {}
    items = data.get('routes') if isinstance(data, dict) else None
    if not isinstance(items, list):
        return set(), {}
    seams: set[tuple[str, str]] = set()
    reasons: dict[tuple[str, str], str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get('route') != 'component':
            continue
        seam = item.get('seam')
        if not isinstance(seam, dict) or seam.get('part') is not None:
            continue
        module = seam.get('module')
        symbols = seam.get('symbols')
        if not isinstance(module, str) or not isinstance(symbols, list):
            continue
        dotted = _normalize_module(module)
        reason = item.get('reason') if isinstance(item.get('reason'), str) else ''
        for symbol in symbols:
            if isinstance(symbol, str) and symbol.strip():
                key = (dotted, symbol.strip())
                seams.add(key)
                if reason:
                    reasons[key] = f'{reasons[key]}\n{reason}' if key in reasons else reason
    return seams, reasons


def _seam_match(module: str, qualified: str, seams: set[tuple[str, str]] | None) -> bool:
    if not seams:
        return False
    klass = qualified.rpartition('.')[0]
    return (module, qualified) in seams or bool(klass and (module, klass) in seams)


def _seam_reason(module: str, qualified: str, reasons: dict[tuple[str, str], str] | None) -> str:
    if not reasons:
        return ''
    parts = [reasons.get((module, qualified), '')]
    klass = qualified.rpartition('.')[0]
    if klass:
        parts.append(reasons.get((module, klass), ''))
    return '\n'.join(part for part in parts if part)


def unstubbed(repository: Path, components: Path, source_root: str,
              only: set[tuple[str, str]] | None = None,
              seams: set[tuple[str, str]] | None = None,
              reasons: dict[tuple[str, str], str] | None = None) -> list[str]:
    """For each documented function of the stubs in `components` that the published stubs
    do not hold, plus any published symbol named in `only` or `seams` (promoted seams),
    the collaborators its code reaches that no stub declares and its docstring (or route
    reason, for a published seam) does not name."""
    reach = Reach(repository / source_root, components)
    problems: list[str] = []
    for stub in sorted(components.rglob('*.pyi')) if components.is_dir() else []:
        module = _stub_module(components, stub)
        try:
            tree = ast.parse(stub.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        published = repository / PUBLISHED / stub.relative_to(components)
        kept = (_qualified(ast.parse(published.read_text(encoding='utf-8')).body)
                if published.is_file() else set())
        for qualified, node in _documented(tree.body):
            if only is not None:
                if (module, qualified) not in only:
                    continue
            elif qualified in kept and not _seam_match(module, qualified, seams):
                continue
            docstring = ast.get_docstring(node) or ''
            if qualified in kept:
                extra = _seam_reason(module, qualified, reasons)
                if extra:
                    docstring = f'{docstring}\n{extra}'
            missing = [name for name in reach.of(module, qualified)
                       if not re.search(rf'\b{re.escape(name.rpartition(".")[2])}\b',
                                        docstring)]
            if missing:
                problems.append(f'{module}.{qualified} reaches {", ".join(missing)}, which '
                                'no stub declares')
    return problems


UNSTUBBED_ADVICE = (
    'A test can fake only what a stub declares, so the test author declines a criterion '
    'whose behaviour passes through any of these. Stub each one the behaviour reaches: '
    'functions with make_stub.py in their own module, and a constant computed at import '
    '(such as a directory read from the environment) in the stub of the module that '
    'uses it, so a test can replace it there. One a test can leave to run as it is, such '
    'as a helper that only computes, is named in the docstring of the symbol that reaches '
    'it, saying so.')


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
                if name in source.imports and not member:
                    # A collaborator the module imports by name, which a test replaces
                    # here, where the code looks it up: re-exported, with its signature
                    # stubbed where it is defined.
                    if not stubber.resolve(name):
                        raise StubError(f'{module}.{name} cannot be stubbed')
                    line = stubber.imports[name]
                    if line.startswith('from ') and ' as ' not in line:
                        stubber.imports[name] = f'{line} as {name}'
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
    problems = unstubbed(repository, components, source_root,
                         {(module, symbol) for symbol in symbols})
    if problems:
        print('\n'.join(problems) + '\n' + UNSTUBBED_ADVICE, file=sys.stderr)
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
    parser.add_argument('--routes', default='routes.json', type=Path)
    arguments = parser.parse_args()
    source_root = arguments.source_root
    if source_root is None:
        try:
            source_root = json.loads(
                (arguments.components / 'runtime.json').read_text())['source_root']
        except (OSError, ValueError, KeyError, TypeError):
            source_root = '.'
    if arguments.check:
        problems = check_published(arguments.repository, arguments.components)
        for problem in problems:
            print(f'make_stub: {problem}', file=sys.stderr)
        if problems:
            print('make_stub: restore each published statement exactly as it was. Add an '
                  'import as a statement of its own (`from typing import Final`), never by '
                  'editing a published one, and state new behaviour in the docstrings of the '
                  'symbols you add, not in a published docstring.', file=sys.stderr)
        seams, reasons = _route_seams(arguments.routes)
        missing = unstubbed(arguments.repository, arguments.components, source_root,
                            seams=seams, reasons=reasons)
        for problem in missing:
            print(f'make_stub: {problem}', file=sys.stderr)
        if missing:
            print(f'make_stub: {UNSTUBBED_ADVICE}', file=sys.stderr)
        if problems or missing:
            return 1
        print('The published stubs are kept, and every collaborator the stubbed symbols '
              'reach is declared or named.')
        return 0
    if not arguments.module or not arguments.symbols:
        parser.error('a module and at least one symbol are required, or --check')
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
