# Validation record — 0.10.0

## 0.10.1 follow-up: no default output budget

Removed default CLI/internal/MCP query output caps and hardcoded agent budget arguments. Explicitly requested caps remain compatible. API-document generation was already uncapped.

Validation: 1,825 tests passed, 17 skipped; lint, package build, generated-skill drift check and skill-frontmatter validation passed. Large-output regressions preserve more than the former 60,000-token-equivalent character limit. Optional MCP HTTP runtime tests are skipped in this environment because its MCP dependency is unavailable; those paths are not counted as runtime-verified.

## Original 0.10.0 validation

Validated locally on Windows, 2026-09-16, on codex/java-api-docs-accuracy.

| Check | Result |
| --- | --- |
| Full pytest suite | 1,822 passed, 17 skipped, 3 warnings |
| Ruff configured checks: graphify, tests, tools | Passed |
| Skill generator drift check | 50 artifacts match generated output and snapshots |
| Source distribution and wheel build | Passed |
| Wheel content inspection | API docs and refreshed Copilot skill included; deleted resolvers absent |
| Public CLI sample | 8 Java files → 26 nodes / 37 edges → strict POST /orders Markdown reference |
| Sample downstream evidence | InventoryRepository.available, PaymentClient.charge, OrderRepository.save retained |
| Graph refresh | graphify update . succeeded; curated graph backup created by existing protection |
| Git whitespace check | Passed |

The warnings concern the existing Hypothesis directory configuration and expected semantic-cache scope checks. Skipped tests were not counted as passed.

These are local regression results, not an enterprise accuracy percentage. The user's enterprise repositories and running Copilot session were not available for direct revalidation in this checkout. Apply the review protocol in ACCURACY.md to those services.
