# Graphify for Java Backend Services

Graphify builds persistent architecture graphs from Java backend repositories.
It extracts Java types, methods, imports, references, inheritance, annotations,
and calls locally with tree-sitter, then exposes the graph to GitHub Copilot,
Codex, Claude, Gemini, and other coding agents.

This distribution indexes `.java` files only. There is no language-selection
flag, semantic document pipeline, or non-Java parser dependency.

## Install

```bash
uv tool install graphifyy
```

Install the agent integration you use:

```bash
graphify copilot install       # GitHub Copilot CLI
graphify vscode install        # VS Code Copilot Chat
graphify codex install         # Codex
graphify claude install        # Claude Code
graphify gemini install        # Gemini CLI
```

## One Java service

```bash
graphify extract ./checkout-service
graphify cluster-only ./checkout-service
```

Outputs are written under `checkout-service/graphify-out/`:

- `graph.json` — machine-readable architecture graph
- `graph.html` — interactive visualization
- `GRAPH_REPORT.md` — architecture summary, hubs, communities, and gaps

Incremental maintenance stays Java-only:

```bash
graphify update ./checkout-service
graphify watch ./checkout-service
```

## Multiple Java microservices

Extract each service independently, then merge from their common parent:

```bash
graphify extract ./checkout-service
graphify extract ./payment-service

graphify merge-graphs \
  ./checkout-service/graphify-out/graph.json \
  ./payment-service/graphify-out/graph.json

graphify cluster-only .
```

`merge-graphs` writes the combined graph to the standard root location:

```text
./graphify-out/graph.json
```

That location matters: installed coding-agent skills check it first. Later
architecture questions query the merged graph instead of rebuilding or reading
only one service.

### Automatic service boundaries

Graphify extracts Spring and Feign HTTP mappings from Java type and method
annotations. During merge it connects a unique outbound repository/client
method to the controller handler with the same HTTP verb and normalized route.
For example, `GET /catalog/addons/{externalId}` matches
`GET /catalog/addons/{id}`. This works for enterprise interfaces such as
`BizCatalogRepository` without a manual bridge.

When route metadata is unavailable, Graphify also connects a unique outbound
`*Client` type to a same-stem `*Controller` type in another repository:

```text
checkout-service
CheckoutController → OrderService → PaymentClient
                                         │
                                         ▼
payment-service
PaymentController → PaymentProcessor → StripeGateway
```

An annotation-matched network-hop edge contains:

```json
{
  "relation": "calls",
  "confidence": "INFERRED",
  "confidence_score": 0.95,
  "cross_service": true,
  "bridge_strategy": "java_http_route",
  "http_method": "GET",
  "http_route": "/catalog/addons/{}"
}
```

The naming fallback edge contains:

```json
{
  "relation": "calls",
  "confidence": "INFERRED",
  "confidence_score": 0.9,
  "cross_service": true,
  "bridge_strategy": "java_client_controller_name"
}
```

Graphify adds an automatic edge only when the controller handler or naming
fallback is unique. It does not guess when multiple services expose the same
HTTP route or controller name.

For nonstandard naming, an optional explicit bridge remains available:

```json
{
  "bridges": [
    {
      "source_repo": "checkout-service",
      "source": "BillingAdapter",
      "target_repo": "payment-service",
      "target": "PaymentEndpoint",
      "relation": "calls"
    }
  ]
}
```

```bash
graphify merge-graphs \
  ./checkout-service/graphify-out/graph.json \
  ./payment-service/graphify-out/graph.json \
  --bridges ./e2e-bridges.json
```

## GitHub Copilot and other agents

After installing the integration, build both service graphs in one request:

```text
/graphify ./checkout-service ./payment-service
```

Then ask questions against the merged graph:

```text
/graphify query How does CheckoutController reach StripeGateway?
/graphify path CheckoutController StripeGateway
/graphify explain PaymentProcessor
```

The graph-first agent workflow runs `graphify query`, `graphify path`, or
`graphify explain` against the existing root `graphify-out/graph.json`. These
explicit commands are graph-only: the agent prefers a graph marked
`graph.graphify_merged: true`, passes its absolute path to the CLI, and does not
fall back to reading Java files. `/graphify query` by itself is incomplete; add
the architecture question after `query`.

## Useful commands

```bash
graphify extract <service>
graphify update <service>
graphify cluster-only <path>
graphify merge-graphs <graph1.json> <graph2.json>
graphify query "<architecture question>"
graphify path "<source type>" "<target type>"
graphify explain "<type or method>"
graphify affected "<type or method>"
graphify export --format graphml
graphify serve
```

## Java extraction guarantees

- Only `.java` files enter the extraction corpus.
- Cross-file type references use packages and imports for disambiguation.
- Receiver-typed member calls resolve only when the target is unique.
- Ambiguous calls and service boundaries are skipped instead of guessed.
- Every relationship carries provenance and confidence metadata.
- Merged node IDs are repository-qualified, preventing cross-service collisions.

## Development

```bash
uv sync --frozen
uv run pytest \
  tests/test_java_member_calls.py \
  tests/test_java_type_resolution.py \
  tests/test_java_only_cli.py \
  tests/test_merge_graphs_cli.py
uv run python -m tools.skillgen --check
```

After changing runtime code, refresh this repository's graph:

```bash
graphify update .
```

## License

Apache-2.0. See `LICENSE`, `LICENSE-MIT`, and `NOTICE`.
