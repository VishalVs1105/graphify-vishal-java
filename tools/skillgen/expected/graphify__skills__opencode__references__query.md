# Java queries and API documentation

```bash
graphify query "Explain GET /orders" --budget 60000 --graph ./service/graphify-out/graph.json
graphify api-docs --graph ./service/graphify-out/graph.json --output ./api-docs --endpoint "GET /orders"
```

Select the graph deliberately. Queries aid navigation and explanation. API documents are deterministic references: endpoint mapping, request/response types and recorded fields, flow diagrams, reachable recorded production call sites, method contracts, conditions, outcomes, and unresolved boundaries.

Conditions are source predicates, not validated business requirements. Unknown feasibility remains unknown. Static reachability does not mean every alternative runs for one request. Neither a high evidence budget nor polished prose repairs missing AST evidence.

Use --strict on api-docs to reject recorded parsing/read failures. It does not certify that every call is resolved. Existing documents are protected unless --force is intentionally supplied.

Let the agent choose the level of detail. Do not automatically copy the entire CLI inventory or impose audience-specific modes. A complete-flow answer must account for downstream service calls and relevant conditions; group them without concealing gaps. Never invent business meanings, required inputs, HTTP statuses, or error mappings.
