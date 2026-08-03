---
inclusion: always
---

graphify: A knowledge graph of this project lives in `graphify-out/`. For codebase, architecture, or dependency questions, when `graphify-out/graph.json` exists, first run `graphify query "<question>"` (or `graphify path "<A>" "<B>"` / `graphify explain "<concept>"`). These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output. Read `GRAPH_REPORT.md` only for broad architecture review or when those commands do not surface enough context.

Explicit `/graphify query`, `/graphify path`, and `/graphify explain` requests are graph-only. Prefer the graph with `graph.graphify_merged: true`, pass its absolute path with `--graph`, and never read `.java` files as a fallback. Ask for missing arguments; report missing graph evidence.
