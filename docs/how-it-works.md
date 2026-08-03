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
Graphify connects a unique same-stem client/controller pair across services,
such as `PaymentClient -> PaymentController`. An explicit bridge contract can
be supplied with `--bridges` when the code uses a different convention.

GitHub Copilot and other installed agent skills use this merged graph for later
questions:

```bash
graphify query "How does checkout reach Stripe?"
graphify path "CheckoutController" "StripeGateway"
graphify explain "PaymentClient"
```

This keeps answers grounded in the merged end-to-end graph instead of making
the agent reconstruct the architecture from source on every question.
