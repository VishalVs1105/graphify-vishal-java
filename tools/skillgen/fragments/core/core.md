@@FRONTMATTER@@

# Graphify — Java API evidence

Use Graphify for Java backend extraction, API documentation, and graph-grounded questions. Prioritize accurate single-repository evidence. Merging services is optional, not a prerequisite.

## Workflow

- `/graphify .` or `/graphify <service>`: run `graphify extract "<service>" --no-cluster`. Java is the default; no LLM key is needed.
- `/graphify update <service>`: run `graphify update "<service>"`.
- `/graphify api-docs`: run `graphify api-docs --graph "<graph.json>" --output "<docs-directory>"`. Add `--endpoint "GET /path"` and `--service "<name>"` when requested. Only add `--force` with overwrite intent.
- `/graphify query <architecture question>`: run `graphify query "<question>" --budget 60000 --graph "<graph.json>"`.
- `/graphify merge <graphs>`: only when requested, run `graphify merge-graphs <graphs...>`; consult the optional merge reference if installed.

## Evidence and presentation

Resolve the graph for the requested repository. Use an explicitly supplied graph first; otherwise locate the project's graphify-out/graph.json. If several graphs could answer the question, ask which scope is intended. Do not automatically prefer a merged graph over a requested single-service graph.

Use the CLI, not a full-file read of a potentially huge graph.json. Inspect small metadata or query output when needed. If evidence is absent or stale, say so and offer re-extraction; do not silently invent missing service hops. Source inspection is appropriate for an explicitly requested accuracy audit or debugging; distinguish source findings from persisted graph evidence.

Choose a clear explanation suited to the user's question. There are no developer/BSA modes or fixed narration templates. Distinguish extracted syntax, inferred targets, unresolved calls, and conditional alternatives. Preserve downstream services and important branches in a complete-flow explanation. Do not present a static inventory as runtime order, concurrency, or a guaranteed execution trace. Do not claim 100% accuracy.

For reviewable API documents, use `graphify api-docs` rather than reconstructing an API contract from method names. You may add a clearly labelled narrative grounded in its evidence. An unresolved call is not proof that the application stops there.

If sidecar references are installed, consult query.md for query/document details and github-and-merge.md only for optional multi-service work.
