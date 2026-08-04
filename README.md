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

If an annotation path is hidden behind a Java constant, merge falls back to an
exact, unique method-name match between the outbound repository/client and a
controller in another service. Generic or ambiguous method names are never
guessed. The API-flow query then continues from that controller through service
interfaces, implementations, repositories, and gateways until the recorded
call chain ends. When one endpoint orchestrates multiple downstream services,
the query keeps the shared controller/service prefix once and renders every
cross-service call as a first-class E2E path in Java call-site order. Route
terms select matching conditional handlers (for example, an addons route
prefers `getAddonEntries`), while exception, unrelated handler, configuration,
DTO/constructor, and mapper/helper noise is omitted or collapsed. After handler
selection, repository/gateway calls outrank local helpers so the rendered path
ends at the recorded data or external boundary instead of a response model.
When a repository method performs multiple operations, each direct call is
shown in Java source order and traversal continues through the operation with
downstream evidence. Installed agent skills require deterministic Java-flow
stdout to be reproduced verbatim so Copilot cannot shorten or rewrite terminals.
They invoke flow queries with `--budget 60000`; traversal correctness is kept
separate from output-size limiting, so the default CLI budget cannot turn an
intermediate eighth hop into a false terminal.

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

API and method flow questions are route-aware and method-directed:

```text
/graphify query Explain the complete flow of POST /payments/charge in payment-service
/graphify query Explain the flow of PaymentProcessor.process in payment-service
```

Graphify maps the exact Spring/Feign route to its controller method, shows the
incoming cross-service bridge, and follows directed method calls downstream.
When the same route or method exists in multiple services it returns an explicit
ambiguity list instead of selecting one by score.

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
