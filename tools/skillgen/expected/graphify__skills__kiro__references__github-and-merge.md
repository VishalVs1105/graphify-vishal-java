# GitHub repositories and Java service merging

Clone a GitHub repository with:

```bash
graphify clone https://github.com/<owner>/<repo>
```

Graphify prints the local checkout path. Build each Java service separately:

```bash
graphify extract ./checkout-service
graphify extract ./payment-service
```

Merge their graphs at the workspace root:

```bash
graphify merge-graphs \
  ./checkout-service/graphify-out/graph.json \
  ./payment-service/graphify-out/graph.json
graphify cluster-only .
```

The default output is `graphify-out/graph.json`. Each node keeps its source
repository. During extraction, Graphify records Spring/Feign HTTP annotations.
During merge, a unique outbound repository/client method is connected to the
controller handler with the same HTTP verb and normalized route. Path-variable
names do not need to match (`/soc/{id}` matches `/soc/{socId}`). This supports
enterprise interfaces such as `BizCatalogRepository` without a manual bridge.

As a fallback, an unambiguous `*Client` class is connected to a same-stem
`*Controller` in another service. For example:

```text
CheckoutController -> OrderService -> PaymentClient
PaymentClient -> PaymentController
PaymentController -> PaymentProcessor -> StripeGateway
```

Route bridges are marked `cross_service=true` with
`bridge_strategy=java_http_route`. Naming fallback bridges use
`bridge_strategy=java_client_controller_name`.

If annotations use constants that cannot be resolved statically or endpoints
are ambiguous, create a bridge contract:

```json
{
  "bridges": [
    {
      "source_repo": "checkout-service",
      "source": "PaymentClient",
      "target_repo": "payment-service",
      "target": "PaymentController",
      "relation": "calls"
    }
  ]
}
```

Then run:

```bash
graphify merge-graphs \
  ./checkout-service/graphify-out/graph.json \
  ./payment-service/graphify-out/graph.json \
  --bridges ./e2e-bridges.json
```

Missing or ambiguous explicit endpoints fail instead of guessing. After the
merge, `/graphify <question>` must query the root graph rather than rebuild a
single service.
