# Graphify Java — AST evidence and API documentation

Graphify Java reads Java backend source and builds a persistent, source-linked graph. Its primary output is a reviewable API reference in Markdown: HTTP mappings, request/response contracts, flow diagrams, method calls, conditions, outcomes, and unresolved boundaries.

This is the Java-focused fork of Graphify. The distribution is named **graphifyy**; the executable is **graphify**. Install from this fork to avoid accidentally running the upstream package.

## Priorities and guarantees

1. Preserve Java syntax evidence and resolve call targets conservatively.
2. Generate accurate, auditable single-service API documentation.
3. Let Copilot or another agent choose the explanation style.
4. Keep multi-service merging as an optional, lower-priority capability.

This is **static analysis, not a Java compiler or runtime tracer**. It cannot guarantee 100% call coverage, exact Spring bean selection, complete JSON serialization schemas, or business meaning. An unresolved invocation is reported as a gap, not as proof that execution stops. An inferred edge is not verified runtime behavior.

## Installation

Prerequisites: Python 3.10+, Git, and uv. Java/Maven/Gradle are not required to parse sources; application dependencies are not executed.

For this development branch:

```powershell
uv tool uninstall graphifyy
uv tool install --force --from "git+https://github.com/VishalVs1105/graphify-vishal-java.git@codex/java-api-docs-accuracy" graphifyy
graphify --version
```

The uninstall step is only needed if a previous uv tool installation exists. If the old install used pip or pipx, remove it through that same environment/manager instead. On Windows, `Get-Command graphify -All` shows which executable is active. Do not install the upstream PyPI package over this fork.

For local development:

```powershell
git clone --branch codex/java-api-docs-accuracy https://github.com/VishalVs1105/graphify-vishal-java.git
cd graphify-vishal-java
uv venv
uv pip install -e .
uv pip install pytest hypothesis ruff build
```

Activate the environment or prefix commands with `uv run`. For reproducible team installs, replace the branch reference with the tested commit SHA.

## Quick start: one enterprise service

Run from the parent directory containing your service:

```powershell
graphify extract .\repoA --no-cluster
graphify api-docs --graph .\repoA\graphify-out\graph.json --output .\api-docs\repoA --strict
graphify query "Explain the complete flow of GET /orders in repoA" --graph .\repoA\graphify-out\graph.json --budget 60000
```

Use the real HTTP route in your service. Extraction automatically ignores non-Java inputs; there is no --java-only flag. The recommended extraction, query, update, and API-doc generation workflow needs **no LLM key**. Omitting --no-cluster can enter the optional backend/clustering workflow and request credentials.

Generate a single endpoint:

```powershell
graphify api-docs --graph .\repoA\graphify-out\graph.json --output .\api-docs\orders --endpoint "POST /orders" --strict
```

After source changes:

```powershell
graphify update .\repoA
graphify api-docs --graph .\repoA\graphify-out\graph.json --output .\api-docs\repoA --strict --force
```

After a parser upgrade, use a **full extraction**, not only an incremental update, to replace old extraction evidence:

```powershell
graphify extract .\repoA --no-cluster --force
```

`extract --out DIR` writes to **DIR/graphify-out/graph.json**. In contrast, `api-docs --output DIR` writes documents directly into **DIR**. Always pass --graph when working with several repositories.

## What the API document contains

- Document control: endpoint owner, declaration location, graph SHA-256, schema version, and review status.
- Request parameters: binding, declared Java type, explicit validation/default/requiredness evidence.
- Response return type and recorded fields of matching request/response types.
- Mermaid flow diagrams with numbered call edges.
- Call-site tables with arguments, conditions, source locations, and evidence classifications.
- Reachable method signatures, recorded decisions, return/throw expressions.
- Unresolved invocation audit, syntax/read diagnostics, and limitations.

Open the Markdown in a Mermaid-capable viewer, such as a repository viewer or an editor with Mermaid support. Large flows are split into multiple diagrams, preserving the listed edges. The tables are the detailed reference; diagrams are not sequence diagrams.

`--strict` rejects recorded parse/read diagnostics; it **does not certify complete target resolution**. Without it, documents retain diagnostic warnings. Existing generated filenames are protected unless --force is supplied. Regeneration does not delete unrelated or obsolete Markdown files; the new index lists the currently selected endpoints.

See the [generated sample](docs/example-api/index.md).

## GitHub Copilot and other agents

Install the bundled integration for your host:

```powershell
graphify copilot install
graphify vscode install
```

Use copilot for Copilot CLI, vscode for the repository's VS Code integration. Install only the host you use; restart/reload its session to pick up changed skills.

Example chat requests:

```text
/graphify .
/graphify api-docs
/graphify query Explain POST /orders and its failure conditions
```

For API documentation, ask the agent to run the api-docs command against the chosen graph. It may then explain the reference in the style you need. **There are no developer/BSA modes or --audience options.** The extraction and graph evidence do not depend on the intended audience.

The agent is instructed to preserve important downstream branches, identify uncertainty, and avoid inventing business rules. The skill cannot guarantee an external model's compliance. Compare its answer to the generated evidence when auditing accuracy.

Other retained integrations include Claude, Codex, generic Agent Skills, Aider, and additional existing installers. Use `graphify --help` for the available host names. Refresh the installed skill after upgrading the package.

## Command guide

| Command | Purpose |
| --- | --- |
| extract SERVICE --no-cluster | Parse Java files and persist local AST evidence |
| update SERVICE | Refresh changed-source graph evidence without LLM extraction |
| api-docs --graph GRAPH --output DIR | Generate endpoint Markdown reference and Mermaid diagrams |
| query QUESTION --graph GRAPH | Retrieve a graph-grounded answer/evidence inventory |
| path SOURCE TARGET --graph GRAPH | Find a graph path; not proof of runtime execution |
| explain SYMBOL --graph GRAPH | Inspect a particular type/method |
| affected SYMBOL --graph GRAPH | Inspect structural impact |
| cluster-only PATH | Optional community analysis of an existing graph |
| export --format graphml | Export using the retained export workflow |
| serve | Optional MCP graph query interface |
| merge-graphs GRAPH1 GRAPH2 ... | Optional multi-service merge and inferred bridges |

Existing report, visualization, database, watch, and integration infrastructure remains for compatibility. It is not required for the primary Java API-doc workflow. Use command help/global help for optional flags; do not assume every legacy command accepts --graph.

## Optional multi-service analysis

First validate the individual service graphs. Then use merge-graphs as before and explicitly select the resulting graph when querying. Repository identities separate same-named classes. Existing route and method-name bridge inference remains available, but **has not been upgraded to compiler/runtime-verified service linkage** in this release.

API docs traverse cross-service calls already stored in the selected graph; generation itself does not fabricate network edges. Missing remote linkage must remain a documented gap.

## Accuracy and troubleshooting

| Symptom | Check |
| --- | --- |
| No LLM key found | Use extract --no-cluster for the local AST workflow |
| Graph file not found | Pass the absolute --graph path; check extract --out nesting |
| Old behavior after upgrade | Check active executable, regenerate graphs, reinstall host skill, restart chat |
| No matching endpoint | Check route spelling/constants, production-source inclusion, selected graph |
| Missing method target | Inspect unresolved calls, imports/types, overloads, generated/external code |
| Large query output | Generate api-docs; do not ask the model to read all of graph.json |
| Strict documentation fails | Fix parsing/read errors, fully re-extract, retry |
| Missing remote service | Validate individual graphs first; review optional inferred bridge evidence |

See [accuracy and review](docs/ACCURACY.md) for a repeatable validation process and known limits.
The [validation record](docs/VALIDATION.md) lists the checks performed on this release.

## Architecture and contributing

Read [the code-level technical guide](docs/TECHNICAL_GUIDE.md) for extraction stages, node/edge fields, call resolution, predicates, persistence, query traversal, document rendering, and extension points.

Read [cleanup and migration notes](docs/CLEANUP.md) for what was removed and retained.

```powershell
python -m pytest -q
python -m ruff check graphify tests tools
python -m tools.skillgen --check
python -m build
```

Edit agent instructions under tools/skillgen/fragments, then run `python -m tools.skillgen` and `python -m tools.skillgen --bless`. Do not hand-edit generated skill copies.

The focused API tests include extraction through the actual public CLI, graph round-tripping, deterministic document generation, recursion, same-name types, wrong arity, varargs, short-circuit conditions, ternary outcomes, and evidence larger than the old 50-item cap.

## Security and licensing

The recommended workflow reads source locally, does not compile/run the Java application, and does not send source to an LLM. Optional remote backends and agent providers have their own data handling policies. Graphs and documents can expose internal architecture, identifiers, and source literals: treat them as sensitive and review before publication. HTML/Markdown escaping is not secret redaction.

Derived from Graphify. Existing Apache-2.0/MIT notices and third-party attribution are retained; see LICENSE, LICENSE-MIT, and NOTICE.
