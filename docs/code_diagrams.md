# Repository code diagrams

The **Code diagrams** GitHub Actions workflow generates architecture diagrams on
pull requests, pushes to `main`, and manual runs from the Actions tab. Download
the `code-diagrams` artifact from a completed run. Start with `modules.svg` for
the overview or `README.md` for the index. Artifacts are retained for 30 days.
The workflow has read-only repository permissions and does not commit output.

The generator uses [Serena's SolidLSP](https://github.com/oraios/serena/tree/v1.5.3/src/solidlsp)
with the Jedi Python language server. It sends a `textDocument/definition`
request at each named function or method call and links the result to a function,
method, or class in the analyzed source. Python's AST supplies syntax, call
ownership, imports, aliases, and public bindings. This is a deterministic script:
it needs no LLM, MCP client, API key, Snowflake connection, or pipeline credentials.
It parses source without importing the pipeline or installing its ML dependencies.

## Output

Each diagram is supplied as Mermaid (`.mmd`), a Markdown Mermaid page (`.md`),
Graphviz (`.dot`), and, in CI, a standalone SVG (`.svg`).

| File | Contents and arrow direction |
| --- | --- |
| `modules.*` | Every analyzed module, with importer → imported module, caller module → callee module, and re-export relationships |
| `imports.*` | Module → dependency, including external import paths |
| `calls.*` | Caller function/method/module → possible function/method/class target resolved by LSP |
| `exports.*` | Module → exported binding |
| `calls-n*.*` | Calls originating in one module, including targets in other modules; indexed by module path in `README.md` |
| `graph.json` | Full nodes, edges, import aliases, source lines, resolution evidence, export modes, and unresolved call sites |

Repeated relationships are collapsed in diagrams; individual call and import
sites remain in JSON. File paths distinguish similarly named symbols. Output is
sorted and has no timestamps or machine-specific absolute paths.

## Run locally

Use Python 3.12 and Git. Install the isolated diagram dependencies:

```sh
uv venv .diagram-tools/venv --python 3.12
```

On Linux/macOS:

```sh
uv pip install --python .diagram-tools/venv/bin/python -r scripts/code-diagrams-requirements.txt
.diagram-tools/venv/bin/python scripts/generate_code_diagrams.py
```

On Windows/PowerShell:

```powershell
uv pip install --python .diagram-tools/venv/Scripts/python.exe -r scripts/code-diagrams-requirements.txt
.diagram-tools/venv/Scripts/python.exe scripts/generate_code_diagrams.py
```

The default output is `artifacts/code-diagrams/`. `--root PATH` selects another
checkout; `--output PATH` changes the output directory (relative to that root).
Use `--include-tests` to add test modules. Source discovery uses `git ls-files`,
so new source files must be staged/committed to be included. It includes tracked
Python in the pipeline, benchmarks, repository tooling, and plugins; it excludes
test directories by default, plugin templates, symlinks, and untracked files.

For local SVG rendering, install Graphviz, then run e.g.:

```sh
dot -Tsvg artifacts/code-diagrams/modules.dot -o artifacts/code-diagrams/modules.svg
```

## Interpretation and limits

- These are static relationships, not observed execution or data lineage.
  Conditional imports, `TYPE_CHECKING` imports, and calls on conditional branches
  remain in the graph. A class call points to the class, not an invented
  constructor-to-method chain.
- LSP can resolve multiple possible targets. Each is retained. Calls through
  registries, runtime monkey patches, dynamic imports, higher-order expressions,
  and some decorators or inferred types may not resolve. External library and
  builtin calls are also outside the internal call graph. These sites appear in
  `graph.json` under `unresolved`; an unresolved count is not a bug count.
- Literal list/tuple `__all__` declarations define exports; otherwise public
  module bindings are inferred, including imported public names. Re-export edges
  connect the exporting module to the origin module and retain the binding name
  in JSON. Computed `__all__` is marked unresolved, and star imports are flagged
  rather than expanded speculatively. This is not a complete Python interpreter.
- Imports are syntax-derived module dependencies, not proof that an optional
  package is installed. Paths absent from the analyzed file inventory are shown
  as dependency nodes. SQL/dbt models, shell scripts, YAML contracts, and data
  assets are outside the Python graph; this does not infer Snowflake table lineage.
- Source parsing, LSP startup, or request failures fail the job. They do not
  silently produce a successful AST-only call graph. Individual requests have
  a 30-second timeout; the workflow has a 30-minute limit.

Serena is pinned to the v1.5.3 commit because SolidLSP is an internal Python API;
Jedi's server version is pinned alongside it. When updating either dependency,
run the real LSP test as well as the generator against the repository.

## Tests

The unit suite uses only Python's standard library:

```sh
python -m unittest discover -s tests -p test_code_diagrams.py -v
```

Set `CODE_DIAGRAMS_LSP_TEST=1` and run that command using the diagram virtual
environment to enable the real Serena integration test. CI always enables it.
It checks cross-file alias and re-export resolution, constructors, and methods.
The unit cases cover unresolved calls, relative imports, exports, scope ownership,
Unicode columns, deterministic output, Git inventory, and failure propagation.
