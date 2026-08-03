---
name: graphify
description: "Build, merge, and query knowledge graphs for Java backend services."
argument-hint: "[service ...|question]"
allowed-tools:
  - read
  - grep
  - glob
  - exec
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
unique outbound repository/client method to a controller handler by HTTP verb
and normalized Spring/Feign route, then falls back to a unique same-stem
`*Client`/`*Controller` pair. Use `--bridges <file.json>` only for unresolved
dynamic routes or an explicit fallback.

When the graph exists, answer codebase questions by running `graphify query
"<question>"`; do not rebuild individual services. Use `graphify path` for a
specific call chain and `graphify explain` for one symbol.
