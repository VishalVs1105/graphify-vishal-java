"""Agent identity changes must not rename the executable or lose user skills."""
from pathlib import Path

import pytest

import graphify.install as installer
from tools.skillgen import gen


@pytest.mark.parametrize("platform", list(installer._PLATFORM_CONFIG) + ["gemini"])
@pytest.mark.parametrize("project", [False, True])
def test_skill_destination_matches_new_identity(platform, project, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    destination = installer._platform_skill_destination(
        platform, project=project, project_dir=tmp_path / "project"
    )
    assert destination.parent.name == "repo-analyzer"
    assert destination.name == "SKILL.md"


def test_generated_skill_names_and_cli_are_consistent():
    for platform in gen.load_platforms().values():
        if platform.bucket == "split":
            core = next(a.content for a in gen.render(platform) if a.path == platform.skill_dst)
            assert "\nname: repo-analyzer\n" in core
            assert "`/repo-analyzer api-docs`" in core
            assert '`graphify api-docs --graph "<graph.json>"' in core
            assert "/graphify " not in core


@pytest.mark.parametrize("vscode", [False, True])
def test_copilot_upgrade_preserves_old_skill_as_backup(tmp_path, monkeypatch, vscode):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / ".copilot" / "skills" / "graphify" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    original = "---\nname: graphify\n---\nUser customization\n"
    legacy.write_text(original, encoding="utf-8")
    (legacy.parent / ".graphify_version").write_text("0.10.1", encoding="utf-8")
    sibling = tmp_path / ".copilot" / "skills" / "other" / "SKILL.md"
    sibling.parent.mkdir()
    sibling.write_text("unrelated", encoding="utf-8")
    if vscode:
        installer.vscode_install(tmp_path)
    else:
        installer.install(platform="copilot")
    new = legacy.parent.parent / "repo-analyzer" / "SKILL.md"
    assert "name: repo-analyzer" in new.read_text(encoding="utf-8")
    assert (new.parent / "references" / "query.md").is_file()
    assert not legacy.exists()
    assert legacy.with_name("SKILL.md.graphify-backup").read_text(encoding="utf-8") == original
    assert sibling.read_text(encoding="utf-8") == "unrelated"
    # A second install is safe and does not overwrite the backup.
    installer._copy_skill_file("copilot")
    assert legacy.with_name("SKILL.md.graphify-backup").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("stamped,backup_exists", [(False, False), (True, True)])
def test_legacy_custom_or_already_backed_up_skill_is_not_changed(
    tmp_path, monkeypatch, stamped, backup_exists
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".copilot" / "skills" / "graphify" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("custom", encoding="utf-8")
    backup = legacy.with_name("SKILL.md.graphify-backup")
    if stamped:
        (legacy.parent / ".graphify_version").write_text("0.10.1", encoding="utf-8")
    if backup_exists:
        backup.write_text("older backup", encoding="utf-8")
    installer._copy_skill_file("copilot")
    assert legacy.read_text(encoding="utf-8") == "custom"
    if backup_exists:
        assert backup.read_text(encoding="utf-8") == "older backup"


def test_failed_install_does_not_retire_legacy_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".copilot" / "skills" / "graphify" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("working old skill", encoding="utf-8")
    (legacy.parent / ".graphify_version").write_text("0.10.1", encoding="utf-8")

    def fail_copy(*args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(installer.shutil, "copy", fail_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        installer._copy_skill_file("copilot")
    assert legacy.read_text(encoding="utf-8") == "working old skill"


def test_project_upgrade_does_not_retire_global_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    legacy_files = []
    for root in (tmp_path / "home", tmp_path / "project"):
        legacy = root / ".copilot" / "skills" / "graphify" / "SKILL.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        (legacy.parent / ".graphify_version").write_text("0.10.1", encoding="utf-8")
        legacy_files.append(legacy)
    installer._copy_skill_file("copilot", project=True, project_dir=tmp_path / "project")
    assert legacy_files[0].is_file()
    assert not legacy_files[1].exists()
    assert legacy_files[1].with_name("SKILL.md.graphify-backup").is_file()
