# Optional GitHub and multi-service graphs

Start with accurate individual Java graphs and review their API documentation. Merging is secondary.

```bash
graphify extract ./repoA --no-cluster
graphify extract ./repoB --no-cluster
graphify merge-graphs ./repoA/graphify-out/graph.json ./repoB/graphify-out/graph.json
```

Use merge-graphs --help for output options. Merging namespaces repository identities so same-named Java classes are not automatically the same entity. Existing HTTP route and method-name bridge heuristics remain inferred evidence, not verified network traces. Review ambiguous or missing connections. Never manufacture a bridge merely to make a path look complete.

Use a merged graph when the user asks for cross-service scope or supplies that graph. Its graph.graphify_merged metadata describes merged provenance; it is not a reason to override a requested single-repository analysis. Do not fetch or publish private source without authorization.
