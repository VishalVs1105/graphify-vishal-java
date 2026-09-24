# Validation record — 0.10.0

## Agent rename branch: repo-analyzer (2026-09-24)

Created `codex/repo-analyzer-agent` from `codex/java-api-docs-accuracy` at `f95c194`. Renamed the agent skill, installation destinations and invocation examples to `repo-analyzer`; retained the `graphify` executable, package identity and graph format.

Validation: full suite **1,875 passed, 17 skipped**, with the same three warning categories recorded below. After the final backup-safety and scope-test additions, the targeted identity/installation/generation suite passed **56 tests**. Ruff, generated-artifact drift checks, skill frontmatter validation, and source/wheel builds passed. Ran `graphify update .` as required by repository instructions. Installer tests use temporary homes/projects, not the user's live agent configuration. Live Copilot command discovery was not exercised.

Upgrade tests cover stamped legacy entrypoint backup, preservation of custom/unmarked skills and existing backups, failure before installation completes, repeat installation, and project-versus-global scope isolation. Existing legacy command/workflow files outside the skill entrypoint are not automatically deleted.

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
