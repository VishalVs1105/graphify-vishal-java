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

For a Java API flow, include the HTTP verb, complete route, and service name:

```text
/graphify query Explain the complete flow of POST /payments/charge in payment-service
```

For a method flow, include `Class.method` and the service name:

```text
/graphify query Explain the flow of PaymentProcessor.process in payment-service
```

These queries return a deterministic `JAVA API FLOW` or `JAVA METHOD FLOW`
section anchored on extracted Spring/Feign route metadata and directed `calls`
edges. The flow follows an interface call into its Java implementation when the
graph contains an `implements` relationship. Test callers and source-less
framework wrappers are omitted from the production business flow. A unique
repository/client-to-controller bridge continues the flow into the downstream
service. If the endpoint orchestrates multiple downstream services, the result
shows the shared endpoint orchestration once and every cross-service call as a
first-class E2E path in Java call-site order. Route/query terms select matching
conditional handlers; unrelated handler alternatives, exception edges, and
configuration, DTO/constructor, or mapper/helper internals are omitted or
collapsed. After handler selection, repository/gateway calls outrank local
helpers so a response model is never presented as the business terminal. A
repository/external terminal means the graph has no further Java call; an
unresolved service leaf means implementation evidence is missing. Return the
entire `JAVA API FLOW` or `JAVA METHOD FLOW` command output verbatim in a fenced
text block before any optional prose. Do not summarize, combine, omit, or
renumber steps. Preserve every service call, source location, confidence marker,
context-filtering note, and exact `Terminal:` line. Before responding, verify
that the last numbered step and each terminal match stdout. If it reports
ambiguity, ask for the service/repository or use one of the listed exact node
IDs; do not guess.
