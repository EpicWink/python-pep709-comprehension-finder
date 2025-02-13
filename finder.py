import ast
import builtins
import sys
import time

from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Generator, Sequence


class ParseError(ValueError):
    pass


def find_709_comps_in_files(path: Path):
    paths: Iterable[Path]
    if path.is_file():
        paths = [path]
    elif path.is_dir():
        paths = path.glob("**/*.py")
    problems = {}
    last_log = 0.0
    for i, path in enumerate(paths):
        if not path.is_file():
            continue
        if time.monotonic() > last_log + 0.8:
            print(f"[{i}] Finding comprehensions in: {path}", file=sys.stderr)
            last_log = time.monotonic()
        problems[str(path)] = find_709_comps_in_file(path)
    return problems


def find_709_comps_in_file(filepath: Path):
    with filepath.open(mode="rb") as fh:
        contents = fh.read()
    try:
        codestr = contents.decode()
    except UnicodeDecodeError as e:
        print((
            f"Could not decode file '{filepath}' with default encoding: "
            f"[{e.__class__.__name__}] {e}"
        ), file=sys.stderr)  # fmt: skip
        return []
    try:
        return find_709_comps(codestr)
    except ParseError as e:
        print((
            f"{e} '{filepath}': [{e.__cause__.__class__.__name__}] {e.__cause__}"
        ), file=sys.stderr)  # fmt: skip
        return []


def find_709_comps(codestr: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(codestr)
    except (SyntaxError, ValueError) as e:
        raise ParseError("Unable to parse file") from e
    finder = CompFinder()
    try:
        finder.visit(tree)
    except RecursionError as e:
        raise ParseError("Recursion error during visiting file") from e
    return finder.problems


class Scope:
    def __init__(self, node: ast.AST | None = None) -> None:
        self.node = node
        self.bound: set[str] = set()
        self.explicit_globals: set[str] = set()
        if node is None:
            self.bound.update(dir(builtins))

    def bind(self, name: str) -> None:
        self.bound.add(name)

    def delete(self, name: str) -> None:
        self.bound.discard(name)

    def is_builtin_scope(self) -> bool:
        return self.node is None

    def is_module_scope(self) -> bool:
        return isinstance(self.node, ast.Module)

    def is_class_scope(self) -> bool:
        return isinstance(self.node, ast.ClassDef)

    def is_bound(self, name: str) -> bool:
        return name in self.bound

    def __repr__(self) -> str:
        if self.is_builtin_scope():
            return "Scope<builtins>"
        match self.node:
            case ast.Module():
                node_desc = "module"
            case ast.ClassDef():
                node_desc = f"class {self.node.name}"
            case ast.FunctionDef():
                node_desc = f"def {self.node.name}()"
            case ast.AsyncFunctionDef():
                node_desc = f"async def {self.node.name}()"
            case _:
                node_desc = self.node.__class__.__name__
        return f"Scope<{node_desc}: {self.bound}>"


class CompFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: list[Scope] = [Scope()]
        # (lineno, name) for each problematic name reference
        self.problems: list[tuple[int, str]] = []
        # if not empty, record name uses and resolved scopes in last dict
        self.resolutions: list[dict[ast.Name, list[Scope | None]]] = []
        self.builtins = set(dir(__builtins__))

    @property
    def current_scope(self) -> Scope:
        return self.scopes[-1]

    @property
    def global_scope(self) -> Scope:
        scope = self.scopes[1]
        assert scope.is_module_scope()
        return scope

    @property
    def builtin_scope(self) -> Scope:
        scope = self.scopes[0]
        assert scope.is_builtin_scope()
        return scope

    @contextmanager
    def nested_scope(self, node: ast.AST) -> Generator[None, None, None]:
        self.scopes.append(Scope(node))
        try:
            yield
        finally:
            self.scopes.pop()

    def visit_in_scope(self, node: ast.AST, body: Sequence[ast.AST]) -> None:
        with self.nested_scope(node):
            for node in body:
                self.visit(node)

    def bind(self, name: str) -> None:
        self.current_scope.bind(name)

    def delete(self, name: str) -> None:
        self.current_scope.delete(name)

    def resolve(self, name: str) -> Scope | None:
        scopes = [self.current_scope, *[s for s in reversed(self.scopes) if not s.is_class_scope()]]
        for scope in scopes:
            if scope.is_bound(name):
                return scope
            if name in scope.explicit_globals:
                return self.resolve_global(name)
        return None

    def resolve_global(self, name: str) -> Scope | None:
        if self.global_scope.is_bound(name):
            return self.global_scope
        if self.builtin_scope.is_bound(name):
            return self.builtin_scope
        return None

    def visit_Module(self, node: ast.Module) -> None:
        self.visit_in_scope(node, node.body)

    def visit_ClassDef(self, node: ast.ClassDef):
        self.bind(node.name)
        self.visit_in_scope(node, node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bind(node.name)
        self.visit_in_scope(node, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bind(node.name)
        self.visit_in_scope(node, node.body)

    def visit_Global(self, node: ast.Global) -> None:
        self.current_scope.explicit_globals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            if node.id in self.current_scope.explicit_globals:
                self.global_scope.bind(node.id)
            else:
                self.bind(node.id)
        elif isinstance(node.ctx, ast.Del):
            self.delete(node.id)
        elif self.resolutions:
            self.resolutions[-1].setdefault(node, []).append(self.resolve(node.id))

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.visit_comp(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.visit_comp(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.visit_comp(node, [node.key, node.value])

    def visit_comp(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp, body: list[ast.AST]
    ) -> None:
        # first visit in pre-709 mode (nested scope)
        self.resolutions.append({})
        with self.nested_scope(node):
            self.visit_comp_inner(node, body)
        # now re-visit in post-709 mode
        self.visit_comp_inner(node, body)
        resolutions = self.resolutions.pop()
        for name_node, res in resolutions.items():
            assert len(res) == 2
            # if this was a NameError pre-709, that's OK
            if res[0] is None:
                continue
            # if this name is bound inside the comprehension, ignore it
            if res[0].node is node:
                continue
            if len(set(res)) > 1:
                self.problems.append((name_node.lineno, name_node.id))

    def visit_comp_inner(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp, body: list[ast.AST]
    ) -> None:
        for i, gen in enumerate(node.generators):
            # outermost iter is evaluated outside comprehension scope
            if i:
                self.visit(gen.iter)
            self.visit(gen.target)
            for ifclause in gen.ifs:
                self.visit(ifclause)
        for el in body:
            self.visit(el)


if __name__ == "__main__":
    results = {}
    for path in sys.argv[1:]:
        results.update(find_709_comps_in_files(Path(path)))
    results_with_problems = {p: r for p, r in results.items() if r}
    print()
    for path, problems in results_with_problems.items():
        print(f"{path}:")
        for lineno, varname in problems:
            print(f"    {lineno} - {varname}")
    if not results_with_problems:
        print(f"Found no files with problems", file=sys.stderr)
