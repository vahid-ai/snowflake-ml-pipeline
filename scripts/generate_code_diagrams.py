"""Build Python architecture diagrams using Serena's SolidLSP and Python syntax.

Source files are parsed, never imported. See docs/code_diagrams.md for scope.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tokenize


DEFAULT_EXCLUDES = ("tests", "templates", "__pycache__")
CALLABLES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def discover(root: Path, include_tests: bool = False) -> list[str]:
    """Use Git's index so environments, generated data and vendored tools stay out."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "ls-files", "-z", "--", "*.py"],
        cwd=root, check=True, capture_output=True,
    )
    excludes = set(DEFAULT_EXCLUDES) - ({"tests"} if include_tests else set())
    return sorted(
        path for path in result.stdout.decode("utf-8").split("\0")
        if path and not excludes.intersection(Path(path).parts)
        and not (root / path).is_symlink()
    )


# Convert tracked source paths into module labels, collapsing package __init__ files to their
# package name.
def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def lsp_column(line: str, byte_offset: int) -> int:
    """Python AST columns are UTF-8 bytes; LSP defaults to UTF-16 code units."""
    prefix = line.encode("utf-8")[:byte_offset].decode("utf-8")
    return len(prefix.encode("utf-16-le")) // 2


# Collect syntax-level ownership, imports, bindings, and call positions without executing
# repository code.
class Source(ast.NodeVisitor):
    # Read the declared source encoding and build one AST inventory for the module.
    def __init__(self, root: Path, path: str):
        self.path = path
        self.module = module_name(path)
        with tokenize.open(root / path) as handle:
            self.text = handle.read()
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text, filename=path)
        self.symbols: list[dict] = []
        self.calls: list[dict] = []
        self.imports: list[tuple[ast.AST, str]] = []
        self.bindings: dict[str, str] = {}
        self.scope: list[str] = []
        self.owner = path
        self.visit(self.tree)

    # Record symbol identity and traverse definition-time expressions separately from deferred
    # function bodies.
    def definition(self, node):
        name = ".".join([*self.scope, node.name])
        identity = f"{self.path}:{node.lineno}:{name}"
        self.symbols.append(dict(id=identity, name=name, path=self.path,
                                 line=node.lineno, kind=type(node).__name__))
        if not self.scope:
            self.bindings[node.name] = identity
        # Decorators, defaults and base classes execute in the enclosing scope.
        for field, value in ast.iter_fields(node):
            if field != "body":
                for child in value if isinstance(value, list) else [value]:
                    if isinstance(child, ast.AST):
                        self.visit(child)
        old_owner = self.owner
        self.owner = identity
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()
        self.owner = old_owner

    visit_FunctionDef = definition
    visit_AsyncFunctionDef = definition
    visit_ClassDef = definition

    def visit_Lambda(self, node):
        # Keep deferred lambda calls separate from calls made by their creator.
        old_owner = self.owner
        self.owner = f"{self.path}:{node.lineno}:lambda@{node.col_offset}"
        self.symbols.append(dict(id=self.owner, name=f"lambda@{node.lineno}:{node.col_offset}",
                                 path=self.path, line=node.lineno, kind="Lambda"))
        self.visit(node.body)
        self.owner = old_owner
        self.visit(node.args)

    # Locate the called name or attribute for LSP lookup, including multiline and Unicode
    # expressions.
    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name):
            line, offset = func.lineno, func.col_offset
        elif isinstance(func, ast.Attribute):
            line = func.end_lineno
            offset = func.end_col_offset - len(func.attr.encode("utf-8"))
        else:
            line, offset = func.lineno, func.col_offset
        self.calls.append(dict(source=self.owner, path=self.path, line=line,
                               column=lsp_column(self.lines[line - 1], offset),
                               expression=ast.get_source_segment(self.text, func),
                               queryable=isinstance(func, (ast.Name, ast.Attribute))))
        self.generic_visit(node)

    # Retain import syntax and enclosing ownership for dependency and re-export analysis.
    def visit_Import(self, node):
        self.imports.append((node, self.owner))
        if not self.scope:
            for alias in node.names:
                self.bindings[alias.asname or alias.name.split(".")[0]] = ""

    visit_ImportFrom = visit_Import

    # Collect module-scope assignments as potential implicit public bindings.
    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store) and not self.scope:
            self.bindings.setdefault(node.id, "")

    # Honor a literal __all__, flag computed exports, or infer public names from module
    # bindings.
    def exports(self) -> tuple[list[str], str]:
        # Exclude nested function/class bodies when inspecting module export declarations.
        def module_nodes(node):
            yield node
            if not isinstance(node, (*CALLABLES, ast.Lambda)):
                for child in ast.iter_child_nodes(node):
                    yield from module_nodes(child)

        statements = list(module_nodes(self.tree))
        assignments = [node for node in statements
                       if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                       and any(isinstance(t, ast.Name) and t.id == "__all__"
                               for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
        if assignments:
            try:
                mutations = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and isinstance(node.func.value, ast.Name) and node.func.value.id == "__all__"
                                for node in statements)
                if (len(assignments) != 1 or isinstance(assignments[0], ast.AugAssign)
                        or assignments[0] not in self.tree.body or mutations):
                    raise ValueError("computed __all__")
                value = ast.literal_eval(assignments[0].value)
                if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
                    raise ValueError("computed __all__")
                return sorted(set(value)), "explicit __all__"
            except (ValueError, TypeError):
                return [], "dynamic __all__: unresolved"
        return sorted(name for name in self.bindings if not name.startswith("_")), "public bindings (implicit)"


# Start the pinned Jedi backend through Serena with local cache paths and bounded request
# timeouts.
@contextmanager
def serena_server(root: Path, cache: Path):
    from solidlsp.ls import SolidLanguageServer
    from solidlsp.ls_config import Language, LanguageServerConfig
    from solidlsp.settings import SolidLSPSettings

    # Also supports invoking the venv's Python directly without activating it.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    settings = SolidLSPSettings(solidlsp_dir=str(cache / "global"),
                               project_data_path=str(cache / "project"))
    server = SolidLanguageServer.create(
        LanguageServerConfig(code_language=Language.PYTHON_JEDI,
                             ignored_paths=[".diagram-tools", "artifacts", ".venv"]),
        str(root), timeout=30, solidlsp_settings=settings,
    )
    with server.start_server_context():
        yield server


# Combine syntax-derived dependencies with callable definitions resolved by the real language
# server.
def build_graph(root: Path, paths: list[str], server) -> dict:
    sources = {path: Source(root, path) for path in sorted(paths)}
    modules = {source.module: path for path, source in sources.items()}
    nodes = {path: dict(id=path, name=source.module, path=path, kind="module", line=1)
             for path, source in sources.items()}
    definitions = {}
    for source in sources.values():
        for symbol in source.symbols:
            nodes[symbol["id"]] = symbol
            if symbol["kind"] != "Lambda":
                definitions[(source.path, symbol["line"])] = symbol["id"]
    edges, unresolved, exports = [], [], []

    # Store typed relationships with source evidence before deterministic sorting.
    def edge(source, target, kind, **extra):
        edges.append(dict(source=source, target=target, kind=kind, **extra))

    # Match analyzed modules or sibling scripts; preserve unmatched names as dependency nodes.
    def import_target(name, path):
        if name in modules:
            return modules[name]
        # Support sibling imports in standalone scripts (e.g. local_iceberg_io).
        sibling = (Path(path).parent / (name.replace(".", "/") + ".py")).as_posix()
        if sibling in sources:
            return sibling
        identity = f"dependency:{name}"
        nodes.setdefault(identity, dict(id=identity, name=name, kind="dependency"))
        return identity

    for path, source in sources.items():
        logging.info("Analyzing %s (%d call sites)", path, len(source.calls))
        names, mode = source.exports()
        for node, owner in source.imports:
            base = ""
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = source.module.split(".")
                    if Path(path).name != "__init__.py":
                        package.pop()
                    base = ".".join(package[:len(package) - node.level + 1] + ([base] if base else []))
            for alias in node.names:
                name = alias.name if isinstance(node, ast.Import) else base
                if base and f"{base}.{alias.name}" in modules:
                    name = f"{base}.{alias.name}"
                target = import_target(name, path)
                edge(path, target, "imports", name=alias.name, alias=alias.asname, line=node.lineno)
                if owner == path and isinstance(node, ast.ImportFrom):
                    binding = alias.asname or alias.name
                    if binding in names and binding != "*":
                        edge(path, target, "reexports", name=binding, line=node.lineno)
                if alias.name == "*":
                    unresolved.append(dict(path=path, line=node.lineno, kind="star_import", expression=base))
        exports.append(dict(path=path, names=names, mode=mode))
        for name in names:
            identity = source.bindings.get(name) or f"{path}:export:{name}"
            nodes.setdefault(identity, dict(id=identity, name=name, path=path, kind="binding"))
            edge(path, identity, "exports", name=name)

        for call in source.calls:
            locations = server.request_definition(path, call["line"] - 1, call["column"]) if call["queryable"] else []
            targets = set()
            for location in locations:
                target_path = location["relativePath"].replace("\\", "/")
                line = location["range"]["start"]["line"] + 1
                if target := definitions.get((target_path, line)):
                    targets.add(target)
            if targets:
                for target in sorted(targets):
                    edge(call["source"], target, "calls", path=path, line=call["line"],
                         expression=call["expression"], evidence="Serena LSP definition")
            else:
                unresolved.append({**call, "kind": "call", "reason": "external, dynamic, or unresolved target"})
    # Stable output enables meaningful diffs; no timestamp or absolute paths.
    return dict(schema_version=1, backend="Serena SolidLSP / Jedi", files=sorted(paths),
                nodes=sorted(nodes.values(), key=lambda n: n["id"]),
                edges=sorted(edges, key=lambda e: json.dumps(e, sort_keys=True)),
                exports=exports, unresolved=unresolved)


# Give Mermaid and Graphviz stable, syntax-safe identifiers independent of display labels.
def node_id(value: str) -> str:
    return "n" + hashlib.sha256(value.encode()).hexdigest()[:16]


# Render the same deduplicated relationships to Mermaid and DOT, escaping source-derived labels.
def diagram(nodes: dict, edges: list[tuple[str, str, str]]) -> tuple[str, str]:
    mermaid, dot = ["flowchart LR"], ["digraph code {", '  rankdir=LR;', '  node [shape=box, fontname="Arial"];']
    for identity, label in sorted(nodes.items()):
        # Numeric entities keep source-derived labels from becoming Mermaid syntax.
        safe = "".join(char if char.isalnum() or char in " ._/-:" else f"#{ord(char)};" for char in label)
        mermaid.append(f'  {node_id(identity)}["{safe}"]')
        dot.append(f"  {node_id(identity)} [label={json.dumps(label)}];")
    for source, target, kind in sorted(set(edges)):
        mermaid.append(f"  {node_id(source)} -->|{kind}| {node_id(target)}")
        dot.append(f"  {node_id(source)} -> {node_id(target)} [label={json.dumps(kind)}];")
    dot.append("}")
    return "\n".join(mermaid) + "\n", "\n".join(dot) + "\n"


# Write the complete JSON graph plus overview, dependency, export, and per-module call views.
def write_outputs(graph: dict, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    (output / "graph.json").write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    nodes = {n["id"]: n for n in graph["nodes"]}
    modules = {n["id"]: n["name"] for n in graph["nodes"] if n["kind"] == "module"}
    module_edges, import_edges, call_edges, export_edges = [], [], [], []
    for edge in graph["edges"]:
        source, target, kind = edge["source"], edge["target"], edge["kind"]
        if kind in {"imports", "reexports"}:
            import_edges.append((source, target, kind))
            if target in modules:
                module_edges.append((source, target, kind))
        elif kind == "calls":
            call_edges.append((source, target, kind))
            a, b = nodes[source]["path"], nodes[target]["path"]
            if a != b:
                module_edges.append((a, b, kind))
        elif kind == "exports":
            export_edges.append((source, target, kind))

    # Qualify symbol labels with file paths to distinguish identical names in separate modules.
    def labels(edges):
        ids = {identity for source, target, _ in edges for identity in (source, target)}
        return {i: (f'{nodes[i]["path"]} :: {nodes[i]["name"]}' if nodes[i]["kind"] not in {"module", "dependency"}
                    else nodes[i]["name"]) for i in ids}

    views = {"modules": (modules, module_edges), "imports": (labels(import_edges), import_edges),
             "calls": (labels(call_edges), call_edges), "exports": (labels(export_edges), export_edges)}
    # Per-module call graphs keep large repositories navigable.
    for path in graph["files"]:
        edges = [e for e in call_edges if nodes[e[0]]["path"] == path]
        if edges:
            views[f"calls-{node_id(path)}"] = (labels(edges), edges)
    report = ["# Repository code diagrams", "",
              "Generated with Serena SolidLSP / Jedi. Arrows point from the importer/caller/exporting module to its target.", "",
              f"Analyzed **{len(graph['files'])} Python files**; **{len(call_edges)} resolved call relationships**; "
              f"**{sum(x['kind'] == 'call' for x in graph['unresolved'])} external or unresolved call sites**.", "",
              "Calls are statically resolved possibilities, not a runtime trace. Imports/exports come from Python syntax. "
              "Public bindings are inferred unless a literal `__all__` is present. See `graph.json` for evidence, aliases, "
              "line numbers, and unresolved sites. SQL/dbt lineage is outside this Python graph.", ""]
    for name, (view_nodes, view_edges) in views.items():
        mmd, dot = diagram(view_nodes, view_edges)
        (output / f"{name}.mmd").write_text(mmd, encoding="utf-8")
        (output / f"{name}.dot").write_text(dot, encoding="utf-8")
        page = f"# {name}\n\n```mermaid\n{mmd}```\n"
        (output / f"{name}.md").write_text(page, encoding="utf-8")
        label = name
        if name.startswith("calls-n"):
            label = next(path for path in graph["files"] if name == f"calls-{node_id(path)}")
        report.append(f"- [{label}]({name}.md) ([Mermaid]({name}.mmd), [Graphviz]({name}.dot))")
    (output / "README.md").write_text("\n".join(report) + "\n", encoding="utf-8")


# Inventory tracked sources, run Serena analysis, and write artifacts only after analysis
# succeeds.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("artifacts/code-diagrams"))
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    output = (root / args.output).resolve()
    paths = discover(root, args.include_tests)
    if not paths:
        parser.error("No tracked Python files found")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("solidlsp").setLevel(logging.ERROR)
    with serena_server(root, root / ".diagram-tools/lsp-cache") as server:
        graph = build_graph(root, paths, server)
    write_outputs(graph, output)
    print(f"Wrote diagrams for {len(paths)} files to {output}")


if __name__ == "__main__":
    main()
