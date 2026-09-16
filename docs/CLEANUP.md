# Cleanup and migration notes

## Removed or simplified

- Deleted the unused non-Java symbol-resolution module and resolver registry: graphify/symbol_resolution.py and graphify/resolver_registry.py.
- Deleted tests devoted to those removed non-Java resolvers; retained the shared ID contract checks.
- Specialized the extraction engine and cross-file resolution module to Java, removing dormant language paths and their unused fact models.
- Removed the non-Java built-in-name suppression list that could hide legitimate Java method calls.
- Removed deterministic BSA prose translation and the developer/BSA query switch. --audience now reports that the option was removed.
- Removed question-keyword pruning from complete Java reachable-call collection.
- Removed heuristic feasibility pruning of complete query calls, outcomes and unresolved invocations; raw arguments and predicates stay available for review.
- Replaced rigid/merge-first agent instructions with single-service evidence and API-doc guidance. Regenerated platform copies from their source fragments.

## Retained intentionally

- Java syntax, imports, contracts, call resolution, graph queries and API-flow infrastructure.
- Existing merge/bridge support, but as an optional secondary workflow.
- Existing graph build/export, clustering, MCP, database, watch and installation infrastructure needed for compatibility.
- Shared semantic/backend modules still referenced by optional legacy CLI features. Removing them blindly would break those paths; they are not used by the recommended Java no-cluster documentation workflow.
- License and attribution files.

No application service repositories or personal presentation/meeting documents are removed. Deleted source files remain recoverable in Git history.

## Breaking/presentation changes

Version 0.10.0 has no developer/BSA output modes. Let the agent decide the level of explanation. Installed skills need reinstalling after an upgrade.

Generate reviewable Markdown with api-docs. Use a new output directory or intentional --force regeneration. Old documents no longer present in the endpoint catalog are not deleted automatically.

Use full extraction after parser upgrades. Merging old graphs cannot recover missing AST call sites or truncated metadata.

## Branch origin

The implementation branch codex/java-api-docs-accuracy was created from the fork's main at 7d00a53. Work from earlier branches remains in Git history.
