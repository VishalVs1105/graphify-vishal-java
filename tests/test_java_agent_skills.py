from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import graphify.__main__ as mainmod
from tools.skillgen import gen


def test_generated_agent_skills_are_java_only_and_query_merged_graph():
    artifacts = gen.render_all(gen.load_platforms())
    skill_bodies = [
        artifact.content
        for artifact in artifacts
        if artifact.path.startswith("graphify/skill")
        and "/references/" not in artifact.path
    ]

    assert skill_bodies
    for body in skill_bodies:
        assert "Java" in body
        assert "graphify merge-graphs" in body
        assert "graphify query" in body
        assert "--java-only" not in body


def test_split_agent_bundles_keep_only_merge_and_query_references():
    platforms = gen.load_platforms()
    for platform in platforms.values():
        if platform.bucket != "split":
            continue
        names = {
            Path(artifact.path).name
            for artifact in gen.render(platform)
            if "/references/" in artifact.path
        }
        assert names == {"github-and-merge.md", "query.md"}


def test_skill_generator_has_no_drift():
    artifacts = gen.render_all(gen.load_platforms())
    assert gen.check(artifacts) == []


def test_copilot_install_contains_java_merge_workflow(tmp_path: Path):
    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with patch("graphify.__main__.Path.home", return_value=tmp_path):
            mainmod.install(platform="copilot")
    finally:
        os.chdir(old_cwd)

    skill = tmp_path / ".copilot" / "skills" / "graphify" / "SKILL.md"
    assert skill.exists()
    body = skill.read_text(encoding="utf-8")
    assert "graphify merge-graphs" in body
    assert "graphify query" in body
    refs = skill.parent / "references"
    assert sorted(path.name for path in refs.iterdir()) == [
        "github-and-merge.md",
        "query.md",
    ]


def test_generic_agent_alias_is_preserved():
    assert mainmod._canonical_platform("skills") == "agents"
    assert mainmod._canonical_platform("agents") == "agents"
