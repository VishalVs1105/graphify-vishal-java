---
name: graphify
description: "Build, merge, and query knowledge graphs for Java backend services."
---

# /graphify

Use Graphify to build and query deterministic knowledge graphs for Java backend
services. Non-Java files are ignored automatically.

## Usage

```text
/graphify
/graphify <java-service>
/graphify <service1> <service2> [service3 ...]
/graphify <question about the existing graph>
```

## Workflow

1. If `graphify-out/graph.json` exists and the user asked an architecture or
   codebase question, do not rebuild. Run:

   ```bash
   graphify query "<question>"
   ```

   Answer only from the returned graph evidence. Say when the graph does not
   contain enough information.

2. For one Java service, build its graph:

   ```bash
   graphify extract <service>
   graphify cluster-only <service>
   ```

3. For multiple Java services, build each graph, then merge them into the
   standard root graph:

   ```bash
   graphify extract <service1>
   graphify extract <service2>
   graphify merge-graphs \
     <service1>/graphify-out/graph.json \
     <service2>/graphify-out/graph.json
   graphify cluster-only .
   ```

   `merge-graphs` writes `graphify-out/graph.json` by default. It automatically
   links an unambiguous Java `*Client` to a same-stem `*Controller` in another
   service, such as `PaymentClient -> PaymentController`.

4. If automatic linking cannot identify a unique endpoint, use an explicit
   bridge contract as described in `references/github-and-merge.md`.

5. After graph creation, use `graphify query`, `graphify path`, or
   `graphify explain`. Subsequent `/graphify` questions must use the merged root
   graph instead of rebuilding individual services.

## GitHub repositories

For GitHub URLs and multi-repository details, load
`references/github-and-merge.md`.

## Querying

For query, path, and explain behavior, load `references/query.md`.

## Rules

- Java is the only extraction mode; invoke commands without a language flag.
- Do not infer missing cross-service edges when names are ambiguous.
- Preserve edge direction and report source locations when available.
- Run `graphify update .` after modifying Java code when a graph already exists.
