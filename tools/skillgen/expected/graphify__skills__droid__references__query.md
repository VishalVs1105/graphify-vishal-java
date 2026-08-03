# Query the merged Java graph

Use the root `graphify-out/graph.json` whenever it exists.

```bash
graphify query "How does checkout call payment?"
graphify path "CheckoutController" "StripeGateway"
graphify explain "PaymentClient"
```

Use `query` for broad context, `path` for a directed end-to-end chain, and
`explain` for one class or method. Base the answer on graph nodes, edges, and
source locations. If no relevant path exists, say so rather than inventing one.
