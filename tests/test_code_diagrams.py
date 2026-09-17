"""Contract tests for architecture extraction; opt-in real Serena integration."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.generate_code_diagrams import (
    Source, build_graph, diagram, discover, lsp_column, serena_server, write_outputs,
)


class FakeServer:
    def __init__(self, locations=None):
        self.locations = locations or {}
        self.requests = []

    def request_definition(self, path, line, column):
        self.requests.append((path, line, column))
        return self.locations.get((path, line, column), [])


def location(path, line):
    return {"relativePath": path, "range": {"start": {"line": line, "character": 0}}}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_alias_call_resolved_by_lsp_not_name_matching(self):
        self.write("a.py", "from b import work as run\ndef main():\n    return run()\n")
        self.write("b.py", "def work():\n    return 1\n")
        server = FakeServer({("a.py", 2, 11): [location("b.py", 0)]})
        graph = build_graph(self.root, ["a.py", "b.py"], server)
        calls = [e for e in graph["edges"] if e["kind"] == "calls"]
        self.assertEqual([(e["source"], e["target"]) for e in calls], [("a.py:2:main", "b.py:1:work")])
        self.assertEqual(calls[0]["evidence"], "Serena LSP definition")

    def test_unresolved_or_variable_targets_are_not_guessed(self):
        self.write("a.py", "handler = registry[name]\nhandler()\nunknown()\n")
        graph = build_graph(self.root, ["a.py"], FakeServer({("a.py", 1, 0): [location("a.py", 0)]}))
        self.assertFalse(any(e["kind"] == "calls" for e in graph["edges"]))
        self.assertEqual(len(graph["unresolved"]), 2)

    def test_relative_imports_and_reexports(self):
        self.write("pkg/__init__.py", "from .worker import run as start\n__all__ = ['start']\n")
        self.write("pkg/worker.py", "def run(): pass\n")
        graph = build_graph(self.root, ["pkg/__init__.py", "pkg/worker.py"], FakeServer())
        self.assertTrue(any(e["kind"] == "imports" and e["target"] == "pkg/worker.py" for e in graph["edges"]))
        self.assertTrue(any(e["kind"] == "reexports" and e["name"] == "start" for e in graph["edges"]))
        self.assertEqual(graph["exports"][0]["names"], ["start"])

    def test_dynamic_all_and_star_imports_are_visible(self):
        self.write("a.py", "from b import *\n__all__ = make_names()\n")
        graph = build_graph(self.root, ["a.py"], FakeServer())
        self.assertEqual(graph["exports"][0]["mode"], "dynamic __all__: unresolved")
        self.assertTrue(any(x["kind"] == "star_import" for x in graph["unresolved"]))

    def test_all_filters_reexports_and_detects_mutations(self):
        self.write("a.py", "from b import visible, hidden\n__all__ = ['visible']\n")
        graph = build_graph(self.root, ["a.py"], FakeServer())
        self.assertEqual([e["name"] for e in graph["edges"] if e["kind"] == "reexports"], ["visible"])
        self.write("a.py", "__all__ = []\n__all__.append('dynamic')\n")
        self.assertEqual(Source(self.root, "a.py").exports(), ([], "dynamic __all__: unresolved"))
        self.write("a.py", "def work():\n    __all__ = ['local']\n")
        self.assertEqual(Source(self.root, "a.py").exports(), (["work"], "public bindings (implicit)"))

    def test_decorators_defaults_and_lambda_owners(self):
        self.write("a.py", "@decorate()\ndef outer(x=default()):\n    return lambda: later()\n")
        source = Source(self.root, "a.py")
        owners = {c["expression"]: c["source"] for c in source.calls}
        self.assertEqual(owners["decorate"], "a.py")
        self.assertEqual(owners["default"], "a.py")
        self.assertIn("lambda@", owners["later"])

    def test_unicode_columns_and_multiline_attributes(self):
        self.assertEqual(lsp_column('"😀"; run()', len('"😀"; '.encode())), 6)
        self.write("a.py", '"😀"; run()\n(obj\n .method)()\n')
        calls = Source(self.root, "a.py").calls
        self.assertEqual((calls[0]["line"], calls[0]["column"]), (1, 6))
        self.assertEqual((calls[1]["line"], calls[1]["column"]), (3, 2))

    def test_duplicate_filenames_remain_distinct_and_output_is_stable(self):
        for path in ["one/tool.py", "two/tool.py"]:
            self.write(path, "def work(): pass\n")
        graph = build_graph(self.root, ["two/tool.py", "one/tool.py"], FakeServer())
        output = self.root / "out"
        write_outputs(graph, output)
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        write_outputs(graph, output)
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
        self.assertEqual(json.loads((output / "graph.json").read_text())["files"], ["one/tool.py", "two/tool.py"])
        self.assertIn("one.tool", (output / "modules.mmd").read_text())
        self.assertIn("two.tool", (output / "modules.mmd").read_text())

    def test_mermaid_labels_cannot_inject_syntax(self):
        mermaid, dot = diagram({"id": 'x"] --> bad["y'}, [])
        self.assertNotIn('"] --> bad["', mermaid)
        self.assertIn('#34;', mermaid)
        self.assertIn('\\"', dot)

    def test_git_inventory_excludes_untracked_templates_and_optional_tests(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for path in ["a.py", "tests/test_a.py", "plugins/templates/example.py", "scratch.py"]:
            self.write(path, "pass\n")
        subprocess.run(["git", "-C", str(self.root), "add", "a.py", "tests", "plugins"], check=True)
        self.assertEqual(discover(self.root), ["a.py"])
        self.assertEqual(discover(self.root, True), ["a.py", "tests/test_a.py"])

    def test_lsp_failures_are_fatal(self):
        self.write("a.py", "work()\n")
        class BrokenServer:
            def request_definition(self, *args):
                raise TimeoutError("LSP unavailable")
        with self.assertRaises(TimeoutError):
            build_graph(self.root, ["a.py"], BrokenServer())

    @unittest.skipUnless(os.environ.get("CODE_DIAGRAMS_LSP_TEST") == "1", "set CODE_DIAGRAMS_LSP_TEST=1 for Serena")
    def test_real_serena_resolves_alias_reexport_and_method(self):
        self.write("pkg/__init__.py", "from .worker import work as exported\n")
        self.write("pkg/worker.py", "def work():\n    return 1\n\nclass Worker:\n    def run(self):\n        return work()\n")
        self.write("app.py", "from pkg import exported as alias\nfrom pkg.worker import Worker\ndef main():\n    alias()\n    worker = Worker()\n    worker.run()\n")
        with serena_server(self.root, self.root / "cache") as server:
            graph = build_graph(self.root, ["app.py", "pkg/__init__.py", "pkg/worker.py"], server)
        calls = {(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == "calls"}
        self.assertIn(("app.py:3:main", "pkg/worker.py:1:work"), calls)
        self.assertIn(("app.py:3:main", "pkg/worker.py:4:Worker"), calls)
        self.assertIn(("app.py:3:main", "pkg/worker.py:5:Worker.run"), calls)
        self.assertIn(("pkg/worker.py:5:Worker.run", "pkg/worker.py:1:work"), calls)


if __name__ == "__main__":
    unittest.main()
