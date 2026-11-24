"""Trusted part of the conformance check: stubtest cannot compare a type alias of a
`Literal` with the code (it reports "is not a Union" even when they agree), so this
compares those aliases itself and lists them for stubtest to leave out.

Usage: literals.py ALLOWLIST MODULE...
"""
import ast
import importlib
import os
import sys
import typing
from pathlib import Path

# The image sets MYPYPATH=/components; a check outside it names the stubs the same way.
STUBS = Path(os.environ.get("MYPYPATH") or "/components")


def _literal(node: ast.expr | None) -> list | None:
    """The values of a `Literal[...]` subscript of constants, else None."""
    if not isinstance(node, ast.Subscript):
        return None
    name = node.value
    if not (isinstance(name, ast.Name) and name.id == "Literal"
            or isinstance(name, ast.Attribute) and name.attr == "Literal"):
        return None
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    constants = [item for item in items if isinstance(item, ast.Constant)]
    if len(constants) != len(items):
        return None
    return [item.value for item in constants]


def _aliases(module: str) -> dict[str, list]:
    path = STUBS.joinpath(*module.split("."))
    stub = path.with_suffix(".pyi") if path.with_suffix(".pyi").is_file() else path / "__init__.pyi"
    aliases = {}
    for statement in ast.parse(stub.read_text()).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        else:
            continue
        values = _literal(value)
        if isinstance(target, ast.Name) and values is not None:
            aliases[target.id] = values
    return aliases


def main(allowlist: str, modules: list[str]) -> int:
    errors, names = [], []
    for module in modules:
        aliases = _aliases(module)
        if not aliases:
            continue
        runtime = importlib.import_module(module)
        for name, values in aliases.items():
            names.append(f"{module}.{name}")
            if not hasattr(runtime, name):
                errors.append(f"{module}.{name} is not present at runtime")
                continue
            value = getattr(runtime, name)
            if typing.get_origin(value) is not typing.Literal or list(typing.get_args(value)) != values:
                errors.append(f"{module}.{name} is inconsistent: the stub has Literal{values}, "
                              f"the code {value!r}")
    Path(allowlist).write_text("".join(f"{name}\n" for name in names))
    print("\n".join(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
