---
description: Build or query a graphify knowledge graph
---

Invoke the `repo-analyzer` skill immediately.

Pass the full `/repo-analyzer` argument string through unchanged.
If no arguments were supplied, treat the target path as `.`.

Examples:
- `/repo-analyzer`
- `/repo-analyzer src --update`
- `/repo-analyzer query "what connects auth to billing?"`

Do not answer from raw files before handing off to the `repo-analyzer` skill.
