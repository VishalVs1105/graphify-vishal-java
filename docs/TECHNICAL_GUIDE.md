# Graphify Java: code-level technical guide

## 1. What this application actually is

Graphify is a Python command-line application using Tree-sitter's Java grammar. It parses source without starting the Java service or resolving its Maven/Gradle dependency graph. NetworkX provides graph storage/traversal and optional analysis. The API-document renderer is deterministic Python; it does not call an LLM.

The extraction result is an evidence model, not bytecode, a compiler symbol table, a complete control-flow graph, or an OpenAPI specification. Copilot consumes the evidence and writes an explanation separately.

## 2. Execution pipeline

```text
Java source files
  → detect.py: discover in-scope files
  → extract.py: orchestrate Java extraction
  → extractors/engine.py: Tree-sitter syntax + local facts
  → extractors/resolution.py and extract.py: bind cross-file evidence
  → call-site aggregation + stable source-relative identities
  → graph.json
      → api_docs.py: endpoint reference + Mermaid + quality audit
      → serve.py: graph queries / path / explanation / optional MCP
```

The primary CLI workflow uses extract --no-cluster. This persists raw evidence with directed endpoints and parallel relationship support. The optional clustered workflow uses the older build/cluster/export pipeline; it is useful for architecture navigation, but is not necessary for API docs. Keep the raw extraction graph as the API evidence source, especially when auditing distinct relationships between the same symbols.

## 3. Entry points and ownership

| Component | Responsibility |
| --- | --- |
| graphify/__main__.py: main | CLI entry, top-level help, agent installation dispatch |
| graphify/cli.py: dispatch_command | extract/update/query/api-docs command routing |
| graphify/detect.py | Java-only file discovery, exclusions, source manifest |
| graphify/extract.py: extract | Per-file parsing, cross-file passes, aggregation, diagnostics |
| graphify/extractors/engine.py: _extract_generic | Java grammar traversal; historical private name retained |
| graphify/extractors/resolution.py | Import/type reference resolution and ID collision handling |
| graphify/security.py: sanitize_ast_metadata | Escape AST metadata without truncating evidence arrays |
| graphify/watch.py | AST-only update/reconciliation and existing-graph protection |
| graphify/api_docs.py | Endpoint catalog, structural traversal, Markdown renderer, CLI |
| graphify/serve.py | Graph loading, graph query logic, interface dispatch, optional MCP |
| tools/skillgen | Single source for agent instructions and generated platform copies |

## 4. File discovery and parsing

collect_files selects .java files and applies the existing noise-directory and path-containment rules. The CLI's detect stage also handles exclusions and incremental manifests. A missing/generated/excluded source cannot contribute an implementation body; this must not be mistaken for an application boundary.

extract_java passes the Java grammar configuration to the engine. The engine creates a Tree-sitter Language and Parser, reads source bytes, and calls parser.parse. Tree-sitter can recover a tree from malformed Java. Recovery is now recorded as java_parse_errors on the file node, with line/column details.

extract returns complete=false with diagnostics for read/parse failures. The normal CLI refuses a failed AST result unless --allow-partial is explicitly requested; partial extraction diagnostics are persisted for strict documentation checks. AST update refuses an incomplete result and preserves the previous graph.

This detects parser/read errors, not Java compilation errors: missing imports, invalid type assignments, and unresolved external dependencies may still be syntactically valid.

## 5. Syntax facts produced by the engine

The declaration pass records:

- Classes, interfaces, enums, records, annotation types, methods, constructors, and containment.
- Package namespace, import relationships, inheritance/implementation and referenced Java types.
- Declared field types; primitive DTO fields are retained as well as reference types.
- Method parameters, return type, signature, parameter count, varargs indicator.
- Recognized HTTP mapping annotations, input bindings and annotation values.
- Decisions and return/throw expressions.

The invocation pass walks method bodies, including nested expressions. Java method_invocation, object_creation_expression, and method_reference nodes produce either a locally bound edge or a raw call fact for resolution. Calls in unsupported/external constructs may remain unresolved rather than guessed.

Method references represent a possible callback target, not proof that the callback executes. Lambda capture and asynchronous scheduling are not equivalent to executing the enclosing method body in source order. Object initialization outside a tracked method/constructor body and generated accessors need additional coverage.

## 6. Identity and same-named types

IDs are anchored to source paths and declaration identities rather than fuzzy display labels. _canonicalize_java_ids rewrites absolute extraction identities to the corpus-relative scheme. _disambiguate_colliding_node_ids isolates remaining source collisions. A bug that could stop prefix matching too early and leak absolute identities was corrected.

Labels are for display; they are not globally unique keys. Two packages can each declare Client. The resolver now checks the caller's explicit import and package namespace before accepting a simple-name candidate. Same-name ambiguity remains unresolved if the evidence cannot choose.

Cross-service namespacing is handled by the retained merge workflow. It does not make same-named DTOs a shared schema automatically. Java inner classes, generated types, complex generic nesting and unusual declaration collisions still require regression coverage; path-based IDs alone are not a complete Java symbol model.

## 7. How member-call resolution works

For a call such as:

```java
payments.charge(request);
```

The parser records the receiver name, candidate declared type, callee, argument count, arguments, source position, and enclosing predicates. _java_method_receiver_types builds receiver information from fields, parameters, and explicit local declarations. Conflicting/shadowed bindings are handled conservatively rather than sharing a global variable-name lookup.

_extract's _resolve_java_member_calls builds type/method indexes and inheritance relationships. Its select_method walks the type hierarchy breadth-first, preferring the nearest declaration. Known argument count filters candidates, including varargs minimum arity. A unique wrong-arity method is no longer accepted simply because its name matches.

For a receiver chain, declared intermediate return types can provide the next receiver type. Qualified imports and same-package context help resolve a type with a duplicated simple name.

This is still **declared-type plus arity resolution**, not Java overload resolution. Argument conversions, generics, boxing, varargs preference, runtime receiver types and dependency bytecode are not comprehensively evaluated. Same-arity overloads stay ambiguous. A possible interface implementation is recorded as inferred dispatch, not compiler-verified bean selection.

## 8. Graph evidence schema

Representative method metadata:

```json
{
  "namespace": "example",
  "java_signature": "create(OrderRequest)",
  "java_parameter_count": 1,
  "java_varargs": false,
  "java_parameters": [
    {"name": "request", "type": "OrderRequest", "binding": "body"}
  ],
  "java_return_type": "ResponseEntity<OrderResponse>",
  "java_http_role": "inbound",
  "java_http_routes": [{"method": "POST", "path": "/orders"}],
  "java_decisions": [],
  "java_outcomes": [],
  "java_unresolved_calls": []
}
```

Strings in persisted metadata are escaped for safe downstream display; the example above shows their decoded meaning. Empty arrays may be absent.

A bound edge has source, target, relation, confidence, source_file, source_location and optional source_column. Repeated calls to the same target are aggregated into a single connectivity edge with occurrence_count and call_sites. Each site retains its own arguments and conditions. Same-line calls are distinguished by column. Recursive calls remain self-edges.

The AST metadata sanitizer no longer imposes the generic semantic sanitizer's 50-item array cap. Removing presentation limits at storage time prevents calls/decisions disappearing before the graph is even queried. No cap removal can recover evidence from graphs generated by older versions: re-extract.

EXTRACTED means syntax/declared-type evidence, not a guarantee that a runtime target was selected. INFERRED means a heuristic relationship such as possible interface dispatch or a remote bridge. Numeric scores are heuristic rankings, **not calibrated accuracy probabilities**.

## 9. Conditions and outcomes

_java_call_conditions walks syntax ancestors and records the branch enclosing an invocation. It handles if/else, ternary branches, loop bodies, switch labels, catch/finally context, and short-circuit right operands.

Important branch meanings:

| Branch | Interpretation |
| --- | --- |
| then | Predicate must be true to enter this syntactic branch |
| else | Predicate false, or recorded default branch |
| after_guard | Earlier true branch exits; reaching this site requires that earlier predicate to have been false |
| after_else_guard | Earlier false branch exits; reaching this site requires the earlier predicate to have been true |
| do_at_least_once | First body entry precedes the repeat condition |
| short_circuit then/else | Right operand is evaluated only after the corresponding left result |

Example:

```java
if (!inventory.available(sku, quantity)) return null;
payments.charge(customerId, quantity);
```

The inventory check itself is not guarded by its own result. The payment call is annotated with the earlier exit predicate and after_guard. The document explains its polarity, rather than displaying a bare condition that could be misread as true.

A predicate invocation inside `allowed() ? accept() : reject()` is not assigned to the false branch. accept and reject carry their distinct branch facts. Return ternaries are expanded into separate recorded outcome expressions, allowing HTTP response expressions to be shown under their actual branches.

These are **syntactic path facts**. There is no whole-program constraint solver, alias analysis, mutation-aware symbolic execution, or proof of feasibility. Earlier guards are historical predicates; intervening assignments can change variables. Traditional switch fallthrough, finally overriding a return, exception propagation, break/continue, and complex nested control flow require manual review. A guard annotation is not a business requirement inferred by an LLM.

## 10. HTTP contracts and DTOs

_java_http_class_info, _java_http_method_mappings, and _java_http_method_metadata recognize supported Java HTTP annotations and combine class/method paths. An unresolved route expression should not be silently replaced with a fabricated route.

_java_parameter_contract records type, input binding, annotations and explicitly available defaults/constraints. The renderer distinguishes unspecified requiredness from a stated constraint. It does not invent a complete validation contract from a DTO name.

_java_method_contract_metadata records declared return types and return/throw expressions. The document reports ResponseEntity expressions instead of inventing exception-to-status mappings. Global ControllerAdvice, security filters, Jackson renaming, inheritance, custom serialization and transitive DTO schemas are not fully modeled.

The current field table reports directly recorded fields for matching endpoint contract types, with their type declaration location. This is not a fully resolved JSON Schema or generated request/response example. Same simple-name DTO candidates can require package-level review.

## 11. Persistence, updates and graph integrity

The raw extraction graph contains nodes and edges; exported graphs can use links. API-document loading accepts both forms, forces serialized source-to-target direction, and uses a MultiDiGraph so distinct relationships are not collapsed by the reader.

The optional older clustered representation is a simple graph; relationships collapsed before persistence cannot be recovered by the document reader. Prefer --no-cluster raw graphs for evidence audits.

The update path reconciles changed source and existing graph state. Failure should preserve existing evidence rather than replace it with an apparently successful partial graph. After parser changes, a full extraction avoids mixing old and new metadata.

Graph SHA-256 in documents identifies the exact input bytes. It is not a source commit hash and does not prove the source checkout is current. Record the application commit, extraction version and command in your review process.

## 12. API-document generation in detail

api_docs.generate:

1. Checks input graph size and loads JSON.
2. Accepts raw edges or exported links without reversing the stored direction.
3. Finds production inbound HTTP mappings with endpoint_catalog.
4. Applies explicit --endpoint/--service selection only.
5. In strict mode, rejects recorded syntax/read diagnostics.
6. Traverses reachable calls and possible interface dispatch through evidence.
7. Renders reference sections and computes the graph SHA-256.
8. Writes stable filenames plus index.md, refusing collisions unless --force.

evidence uses an iterative queue and a visited-node set. Recursive edges remain in the output but do not cause endless traversal. It follows all recorded production call alternatives, including stored cross-service edges. It does **not** prune by words in the question or fabricate remote links.

Traversal is approximately O(V+E) after indexing the graph for an endpoint. Rendering all endpoints currently repeats indexing/traversal, so total work can approach endpoint count multiplied by graph size; this is an optimization opportunity for very large services. No parallel parsing claim is made: extract currently loops over Java files.

Mermaid diagrams use stable local node IDs and numbered C-edge labels. Diagrams split every 35 edges for readability. The call-site table preserves repeated invocations, argument expressions and predicates. Ordering is for presentation, not Java evaluation order.

## 13. Queries, agents and merging

serve.py's Java query formatter remains useful for interactive navigation. It includes curated paths and a complete reachable inventory. The question-wording filter that removed unrelated-named downstream methods has been removed from reachable-call collection.

The API-document renderer is the review artifact when complete recorded alternatives matter. It bypasses query narration heuristics and token budgets.

Agent instructions no longer impose BSA/developer modes, a mandatory narration template, or automatic merged-graph preference. The agent selects presentation depth while preserving evidence and uncertainty. Installation copies generated skill files; upgrading Python code does not automatically refresh every installed host skill.

Merging remains compatible and secondary. Existing route and method-name heuristics infer remote bridges. They are not a substitute for endpoint/configuration verification, and api-doc generation does not make those edges more certain.

## 14. Security and operational boundaries

Source is read, not executed, in the recommended no-cluster workflow. No build hooks, Maven plugins, Java application startup, or LLM calls are needed. Optional agents/backends can transmit their own input under their own policies.

Escaping mitigates markup injection in generated references; it does not remove secrets. Source literals, internal URLs, method names and branch expressions can be sensitive. Keep raw graphs and docs under the application's access controls.

Very large methods and graphs now retain more evidence, increasing memory/disk requirements. Keep graph-size checks and evaluate representative large services. No enterprise throughput or SLA is claimed without measurements.

## 15. Architect questions: precise answers

**Is this a full AST or regex tool?** Tree-sitter creates a Java syntax tree. Python walks named nodes and uses small normalization helpers/regexes for identifiers, annotations and display. Semantic resolution is custom and partial.

**Why can it miss a call?** The source may be absent/excluded/generated, receiver types may be unresolved, overloads ambiguous, or a construct unsupported. Recorded unresolved calls expose some gaps; they do not prove every syntax form was observed.

**Can it explain all conditions?** It records supported syntactic conditions and outcomes. It does not solve every runtime path or derive all business semantics. Full compiler/CFG and runtime evidence would be needed for stronger guarantees.

**Why not promise 100% accuracy?** Tests prove behavior on bounded fixtures. Java reflection, frameworks, external libraries and runtime configuration exceed the current static model.

**What does the LLM do?** Nothing in the primary extraction/docs path. Copilot can summarize and contextualize the deterministic reference.

**How should accuracy improve next?** Add a labeled enterprise corpus, measure call-site and target precision/recall separately, then prioritize unresolved categories. Compiler-backed symbol resolution, richer CFG/exception modeling and framework adapters are future work, not implemented claims.

**Why is merging last?** A multi-service graph amplifies inaccurate individual edges. Validate extraction and per-service contracts first; add remote linkage only with evidence.

## 16. Extending safely

Add a source fixture and a failing assertion before changing resolution. Test both positive and negative matches: duplicate names, wrong arity, overloads, tests vs production, and unresolved framework boundaries. Then test raw JSON and API-doc output through the real CLI. Avoid domain-name shortcuts and never change reachability simply to improve one screenshot.

See [ACCURACY.md](ACCURACY.md) for the release/review checklist.
