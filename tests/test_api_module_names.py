"""Every name an API route uses must actually resolve.

THE BUG THIS CATCHES, WHICH HAS NOW HAPPENED THREE TIMES
--------------------------------------------------------
`backend/api/admin.py` used `settings` in three routes and never imported it:

    GET  /api/admin/trading-mode           -> 500 NameError
    POST /api/admin/live-trading/enable    -> 500 NameError
    POST /api/admin/live-trading/disable   -> 500 NameError  <- the dangerous one

The disable route raised on its FIRST statement, so the documented way to turn
real-money trading OFF over HTTP was dead. It failed safe — the flag never
changed — but it failed.

The same class had already hit this codebase twice before, and both are recorded
in the modules' own docstrings: `api/dashboard.py` used `logging`,
`get_message_bus` and `get_portfolio` without importing them, and `api/ai.py`
used nine names it never imported. Both took the whole backend down at import.

WHY NORMAL TESTING DOES NOT CATCH IT
------------------------------------
A missing import inside a FUNCTION body is not an import-time error. The module
imports cleanly, the app starts, the route registers, and the OpenAPI schema
lists it. It fails only when someone calls it — and nothing imported these three
functions, so no test ever did. It surfaced from sweeping every endpoint against
a running server.

WHAT THIS CHECKS
----------------
For every module under `backend/api/`, every `name.attribute` access inside a
function is resolved against that function's local bindings, its enclosing
scopes, the module's real namespace, and builtins. Anything left over is a name
that will raise NameError the moment that line executes.

Deliberately narrow: only bases of ATTRIBUTE accesses, which is where this bug
lives (`settings.LIVE_TRADING`, `logging.getLogger`, `os.getenv`). A general
undefined-name checker is pyflakes' job, and re-implementing it here would trade
a real guard for a stream of false positives nobody keeps green.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pathlib

import pytest

API_DIR = pathlib.Path("backend/api")


def _module_names(path: pathlib.Path) -> list[str]:
    return [p.stem for p in sorted(path.glob("*.py")) if p.stem != "__init__"]


class _ScopeCollector(ast.NodeVisitor):
    """Collect names bound anywhere in one function's own scope.

    Intentionally over-collects: a name bound in a nested branch still counts as
    bound. Over-collecting can only cause a FALSE NEGATIVE (a real bug slipping
    through), never a false positive — and a check that cries wolf is one that
    gets deleted.
    """

    def __init__(self) -> None:
        self.bound: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self.bound.add(node.arg)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bound.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.bound.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.bound.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.bound.update(node.names)


def _attribute_bases(fn: ast.AST) -> set[str]:
    """Names used as `name.attr` inside this function, at any depth."""
    bases: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if isinstance(node.value.ctx, ast.Load):
                bases.add(node.value.id)
    return bases


def _functions(tree: ast.AST):
    """Every function, with the chain of scopes enclosing it.

    Nested functions inherit their parents' bindings — a closure over a name
    defined in the outer function is legal and must not be flagged.
    """
    def walk(node, enclosing: list[ast.AST]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, list(enclosing)
                yield from walk(child, enclosing + [child])
            else:
                yield from walk(child, enclosing)

    yield from walk(tree, [])


@pytest.mark.parametrize("module_name", _module_names(API_DIR))
def test_every_attribute_base_in_an_api_module_resolves(module_name):
    module = importlib.import_module(f"backend.api.{module_name}")
    source = (API_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_names = set(vars(module)) | set(dir(builtins))

    unresolved: list[str] = []
    for fn, enclosing in _functions(tree):
        own = _ScopeCollector()
        own.visit(fn)

        visible = set(own.bound) | module_names
        for scope in enclosing:
            outer = _ScopeCollector()
            outer.visit(scope)
            visible |= outer.bound

        for base in _attribute_bases(fn) - visible:
            unresolved.append(f"{fn.name}() uses '{base}.…' but '{base}' is not defined")

    assert not unresolved, (
        f"backend/api/{module_name}.py has name(s) that will raise NameError when the "
        f"route runs:\n  " + "\n  ".join(sorted(set(unresolved)))
    )
