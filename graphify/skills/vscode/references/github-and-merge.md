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
repository. During merge, an unambiguous `*Client` class is connected to a
same-stem `*Controller` in another service. For example:

```text
CheckoutController -> OrderService -> PaymentClient
PaymentClient -> PaymentController
PaymentController -> PaymentProcessor -> StripeGateway
```

The bridge edge is directed and marked `cross_service=true` with
`bridge_strategy=java_client_controller_name`.

If naming does not match or endpoints are ambiguous, create a bridge contract:

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
