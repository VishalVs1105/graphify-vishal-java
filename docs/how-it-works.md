# How Graphify works

Graphify scans Java backend services, parses `.java` files with tree-sitter,
and emits nodes for source files, classes, methods, and relevant members. It
emits directed relationships for containment, calls, construction, inheritance,
imports, and resolved type usage.

Code files are not sent to the LLM semantic extractor; Java extraction is local
and deterministic.

For a single service:

```bash
graphify extract ./checkout-service
```

For multiple services:

```bash
graphify extract ./checkout-service
graphify extract ./payment-service
graphify merge-graphs \
  ./checkout-service/graphify-out/graph.json \
  ./payment-service/graphify-out/graph.json
```

The merge writes `graphify-out/graph.json` at the current workspace root.
Graphify first matches unique outbound repository/client methods to controller
handlers using HTTP verb and normalized Spring/Feign route metadata. Variable
names are ignored, so `/addons/{externalId}` matches `/addons/{id}`. When route
metadata is unavailable, Graphify falls back to a unique same-stem
client/controller pair such as `PaymentClient -> PaymentController`. An
explicit bridge contract remains available for dynamic or ambiguous routes.

GitHub Copilot and other installed agent skills use this merged graph for later
questions:

```bash
graphify query "How does checkout reach Stripe?"
graphify query "Explain POST /payments/charge" --audience developer --budget 60000
graphify query "Explain POST /payments/charge" --audience bsa --budget 60000
graphify path "CheckoutController" "StripeGateway"
graphify explain "PaymentClient"
```

This keeps answers grounded in the merged end-to-end graph instead of making
the agent reconstruct the architecture from source on every question.

The developer audience includes the complete reachable production-call
inventory plus per-method request/response contracts and AST-extracted branch
conditions. The BSA audience uses the same graph evidence but renders request
meaning, business operations, decision rules, service interactions and outcomes
without exposing Java chains or DTO names/types. Older service graphs must be
rebuilt and re-merged before these enriched sections are available.

Java flow analysis also follows receiver chains through declared return types,
resolves inherited fields/methods, records method references, carries call
arguments into downstream parameters, and recognizes conditions implied by an
earlier terminating guard clause. When an enum/literal argument selects a
switch branch, impossible alternatives are excluded consistently from the
architectural path, completeness inventory, BSA rules, and outcomes.
