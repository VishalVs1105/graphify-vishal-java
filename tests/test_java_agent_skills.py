from pathlib import Path
import os
from unittest.mock import patch

import graphify.__main__ as mainmod
from tools.skillgen import gen


def test_generated_agent_skills_prioritize_java_api_docs():
    artifacts = gen.render_all(gen.load_platforms())
    bodies = [
        a.content
        for a in artifacts
        if a.path.startswith("graphify/skill") and "/references/" not in a.path
    ]
    assert bodies
    for body in bodies:
        for required in (
            "Java",
            "graphify api-docs",
            "graphify query",
            "--graph",
            "Merging services is optional",
            "unresolved",
            "no developer/BSA modes",
        ):
            assert required in body, required
        assert "--audience" not in body
        assert "--java-only" not in body
        assert "Do not automatically prefer a merged graph" in body


def test_split_agent_bundles_keep_query_and_optional_merge_references():
    for platform in gen.load_platforms().values():
        if platform.bucket == "split":
            names = {Path(a.path).name for a in gen.render(platform) if "/references/" in a.path}
            assert names == {"github-and-merge.md", "query.md"}


def test_skill_generator_has_no_drift():
    assert gen.check(gen.render_all(gen.load_platforms())) == []


def test_copilot_install_contains_api_docs_workflow(tmp_path):
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch("graphify.__main__.Path.home", return_value=tmp_path):
            mainmod.install(platform="copilot")
    finally:
        os.chdir(old_cwd)
    skill = tmp_path / ".copilot" / "skills" / "repo-analyzer" / "SKILL.md"
    body = skill.read_text(encoding="utf-8")
    assert "graphify api-docs" in body
    assert "no developer/BSA modes" in body
    assert "Do not automatically prefer a merged graph" in body
    query = (skill.parent / "references" / "query.md").read_text(encoding="utf-8")
    assert "--strict" in query
    assert "Never invent" in query


def test_generic_agent_alias_is_preserved():
    assert mainmod._canonical_platform("skills") == "agents"
    assert mainmod._canonical_platform("agents") == "agents"
