# Architecture

Graphify is a Python CLI that builds deterministic knowledge graphs from Java
backend services and exposes those graphs to GitHub Copilot and other coding
agents.

## Runtime flow

```text
.java files
    -> Java tree-sitter extraction
    -> classes, methods, fields, calls, and type references
    -> NetworkX graph construction
    -> optional clustering/report/export
    -> graphify-out/graph.json
```

`graphify extract <service>` scans only `.java` files. `graphify update
<service>` and watch/hooks use the same scope.

## Multi-service flow

Each service owns an independent `graphify-out/graph.json`. `graphify
merge-graphs` namespaces nodes by repository and combines the graphs. Java
extraction persists Spring/Feign HTTP verb and route metadata on methods. Merge
adds a directed cross-service edge when an outbound repository/client method
has exactly one controller handler in another repository with the same verb and
normalized route. Path-variable names are normalized:

```text
BizCatalogRepository.getAddon() -> AddonController.getAddon()
```

When route metadata is unavailable, merge falls back to exactly one same-stem
Java client and controller pair. Before the class-level fallback, an exact and
unique outbound repository-method/controller-method name match is used; this
covers annotation routes stored in constants:

```text
PaymentClient -> PaymentController
```

The merged graph is written to the workspace-root
`graphify-out/graph.json`. Explicit bridge JSON remains available for dynamic
routes whose method names also differ; ambiguous endpoints fail instead of
being guessed.

## Agent integration

The package ships `/graphify` skill bundles for GitHub Copilot, VS Code
Copilot Chat, Codex, Claude, and the other existing agent targets. Their shared
workflow is:

1. Build one graph per Java service.
2. Merge service graphs into the standard root graph.
3. Reuse that root graph for `query`, `path`, and `explain` requests.

The skill sources live under `tools/skillgen/fragments/`; generated packaged
artifacts live under `graphify/skill*.md` and `graphify/skills/`.

## Main modules

- `graphify/extract.py`: Java-only file collection and extraction façade.
- `graphify/extractors/engine.py`: tree-sitter graph extraction engine used by
  the Java configuration.
- `graphify/build.py`: graph normalization, merging, and incremental updates.
- `graphify/bridges.py`: explicit and automatic cross-service bridges.
- `graphify/cli.py`: extract, merge, query, path, explain, and export commands.
- `graphify/watch.py`: Java-only update/watch integration.

Graph data is plain JSON and NetworkX data; generated state stays inside
`graphify-out/`.
