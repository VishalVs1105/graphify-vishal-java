---
name: graphify
description: "Build, merge, and query knowledge graphs for Java backend services."
---

# /graphify

Graphify extracts `.java` files only. For one service run `graphify extract
<service>`. For multiple services, extract each service and run:

```bash
graphify merge-graphs \
  <service1>/graphify-out/graph.json \
  <service2>/graphify-out/graph.json
graphify cluster-only .
```

The merged graph is `graphify-out/graph.json`. Graphify automatically links a
unique same-stem `*Client` in one service to a `*Controller` in another. Use
`--bridges <file.json>` only when an explicit fallback is needed.

When the graph exists, answer codebase questions by running `graphify query
"<question>"`; do not rebuild individual services. Use `graphify path` for a
specific call chain and `graphify explain` for one symbol.
