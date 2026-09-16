# Accuracy and review protocol

## What is verified

The repository contains bounded Java regression fixtures. They test behavior, not a universal enterprise accuracy percentage.

The new API-document tests cover:

| Area | Regression assertion |
| --- | --- |
| Large methods | 68 calls and 65 conditions survive storage without the former 50-item truncation |
| Same-line calls | Separate columns preserve repeated invocation sites |
| Query wording | Developer/BSA/no-audience wording does not remove alternatives from the complete inventory |
| Unknown runtime values | Request null checks cannot hide error outcomes, downstream calls or unresolved invocations |
| Ternary logic | Predicate evaluation is not falsely attached to the else branch |
| Short-circuit logic | Right operand carries the left predicate; return alternatives remain separate |
| Duplicate type names | Explicit package imports select the corresponding type |
| Wrong arity | A unique same-name method is not accepted with the wrong argument count |
| Varargs | Zero and multiple arguments remain resolvable for a unique varargs member |
| Recursion | Self-call retained; document traversal terminates |
| Raw JSON | edges/links input preserves stored direction |
| Document generation | Stable output, field types, graph checksum, overwrite protection |
| Public CLI | Real extraction of sample service followed by strict API-doc generation |
| Diagnostics | Missing/malformed Java is not silently certified complete |

Run:

```powershell
python -m pytest tests/test_java_api_docs.py tests/test_java_member_calls.py tests/test_java_type_resolution.py tests/test_java_business_logic.py -q
python -m pytest -q
python -m tools.skillgen --check
```

## Validate your enterprise service

1. Pin the application commit and Graphify commit/version.
2. Fully extract with --no-cluster --force into a dedicated output location.
3. Generate api-docs with --strict.
4. Select endpoints covering your actual patterns: synchronous calls, async callbacks, generics, overloads, inheritance, HTTP clients, validation, guards, switches, exceptions, caching.
5. Have a developer label every invocation site and expected target from source. Keep line/column and caller identity, not just method names.
6. Compare the graph's call_sites and java_unresolved_calls against that ledger.
7. Check false-positive target bindings independently from missed calls.
8. Review conditions against source, especially predicate polarity, mutation, fallthrough, callbacks and exceptions.
9. Check request/response contracts against framework behavior and API tests.
10. Compare Copilot's explanation with the generated document; record explanation omissions separately from extraction omissions.

Suggested ledger columns: endpoint, caller, source file, line, column, invocation text, expected target(s), graph target(s), condition, expected outcome, classification, reviewer, application commit, Graphify version.

## Use separate metrics

- Invocation observation recall = observed in-scope invocation sites / manually labeled in-scope invocation sites.
- Target precision = correct resolved target bindings / all asserted resolved target bindings.
- Target recall = correct resolved target bindings / expected resolvable target bindings.
- Condition fidelity = correctly represented sampled predicates/polarities / manually reviewed sampled predicates.
- Documentation retention = represented recorded call sites / reachable recorded call sites.
- Explanation retention = material documented facts retained by the agent / material facts selected for review.

Define the denominator and treatment of external/generated calls before reporting numbers. A high resolved-call count does not prove high accuracy. Heuristic edge scores are not probabilities.

## Known limitations

- No compiler classpath, full type inference, dynamic dispatch proof, Spring bean-selection proof or reflection analysis.
- No whole-program symbolic execution, mutation-aware control-flow proof, complete exception graph or runtime ordering.
- Traditional switch fallthrough, complex break/continue/finally, initializer execution, generated methods and library bodies require review.
- DTO field output is not a full Jackson/OpenAPI schema; requiredness and HTTP error mappings can remain unspecified.
- Raw no-cluster evidence is preferred; a previously collapsed simple-graph relationship cannot be reconstructed later.
- Query summaries and agent explanations can omit details; generated docs expose the retained call-site evidence.
- Optional remote bridges remain inferred; no new merge-accuracy guarantee is made.

## Migration from older graphs

A query or larger token budget cannot restore metadata lost during an older extraction. Re-extract every service after installing the new parser. Rebuild optional merged graphs from those new inputs. Regenerate Markdown and refresh the agent's installed skill.

If a strict run fails, fix the diagnostics; do not use --allow-partial to obtain a clean-looking production reference. Partial graphs are for investigation and must retain their warnings.
