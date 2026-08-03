# Query the merged Java graph

Use the root `graphify-out/graph.json` whenever it exists. If multiple graphs
are present, select the one whose JSON metadata has
`graph.graphify_merged: true`, resolve its absolute path, and pass it explicitly.

```bash
graphify query "How does checkout call payment?" --graph "<absolute-merged-graph.json>"
graphify path "CheckoutController" "StripeGateway" --graph "<absolute-merged-graph.json>"
graphify explain "PaymentClient" --graph "<absolute-merged-graph.json>"
```

Use `query` for broad context, `path` for a directed end-to-end chain, and
`explain` for one class or method. Base the answer only on graph nodes, edges,
and source locations. For an explicit `/graphify query`, `/graphify path`, or
`/graphify explain`, do not read or search `.java` files as a fallback. If the
command is missing its question/operands, ask for them. If no relevant evidence
or path exists, say so and recommend rebuilding or re-merging the graph.
