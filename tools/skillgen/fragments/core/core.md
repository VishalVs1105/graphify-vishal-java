@@FRONTMATTER@@

# /graphify

Use Graphify to build and query deterministic knowledge graphs for Java backend
services. Non-Java files are ignored automatically.

## Usage

```text
/graphify
/graphify <java-service>
/graphify <service1> <service2> [service3 ...]
/graphify <question about the existing graph>
/graphify query <architecture question>
/graphify path <source type> <target type>
/graphify explain <type or method>
```

## Explicit graph commands

Treat `/graphify query`, `/graphify path`, and `/graphify explain` as strict
graph-only requests:

1. Require the question or operands shown above. If they are missing, ask for
   them and do not inspect source files.
2. Locate `graphify-out/graph.json` from the workspace root and its parent
   workspace directories. When more than one graph exists, prefer the graph
   whose JSON metadata has `graph.graphify_merged` set to `true`.
3. Pass that graph explicitly with `--graph "<absolute-path>"`.
4. Answer only from command output. Do not open, search, or infer from `.java`
   files. If the graph lacks the answer, report insufficient graph evidence and
   recommend rebuilding or re-merging it.

## Workflow

1. If `graphify-out/graph.json` exists and the user asked an architecture or
   codebase question, do not rebuild. Run:

   ```bash
   graphify query "<question>" --graph "<absolute-path-to-merged-graph.json>"
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

   `merge-graphs` writes `graphify-out/graph.json` by default. It first matches
   unique outbound repository/client methods to controller handlers by HTTP
   verb and normalized Spring/Feign route. It also links an unambiguous Java
   `*Client` to a same-stem `*Controller`, such as
   `PaymentClient -> PaymentController`.

4. If automatic linking cannot identify a unique endpoint, use an explicit
   bridge contract as described in `references/github-and-merge.md`.

5. After graph creation, use `graphify query`, `graphify path`, or
   `graphify explain`, always with `--graph` pointing at the merged root graph.
   Subsequent `/graphify` questions must use that merged graph instead of
   rebuilding or reading individual services.

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
