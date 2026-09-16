---
inclusion: always
---

graphify: A knowledge graph of this project lives in `graphify-out/`. For codebase, architecture, or dependency questions, when `graphify-out/graph.json` exists, first run `graphify query "<question>"` (or `graphify path "<A>" "<B>"` / `graphify explain "<concept>"`). These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output. Read `GRAPH_REPORT.md` only for broad architecture review or when those commands do not surface enough context.

- Explicit Graphify queries use the graph for the requested repository, passed with --graph. Merging is optional; do not automatically prefer merged scope. Generate reviewable Java API references with graphify api-docs. Let the agent choose presentation style; distinguish recorded evidence from source audits and inference.
