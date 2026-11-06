from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "scripts" / "install-codex.sh"
SKILL_DIR = ROOT / "skills" / "agent-board"


def _version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S).group(1)
    return re.search(r'^version\s*=\s*"([^"]+)"', section, re.M).group(1)


def test_plugin_manifest_has_required_fields_and_skill_resolves():
    # arrange
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    # act
    skill_dirs = [(ROOT / p).resolve() for p in manifest["skills"]]

    # assert
    assert manifest["name"] == "agent-board"
    assert manifest["version"] == _version()
    assert manifest["description"]
    assert any((d / "agent-board" / "SKILL.md").is_file() for d in skill_dirs)


def test_marketplace_lists_the_plugin_with_local_source():
    # arrange
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    # act
    entry = market["plugins"][0]

    # assert
    assert market["name"] and market["owner"]["name"]
    assert entry["name"] == "agent-board" and entry["source"] == "./"
    assert entry["version"] == _version()
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()


def test_harness_references_exist_and_are_linked_from_skill():
    # arrange
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    # act / assert
    for name in ("harness-claude.md", "harness-codex.md"):
        assert (SKILL_DIR / "references" / name).is_file()
        assert "references/" + name in text


def _run(codex_home, *args):
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    return subprocess.run(["sh", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=30)


def test_install_codex_links_is_idempotent_and_uninstalls(tmp_path):
    # arrange
    link = tmp_path / "skills" / "agent-board"

    # act
    first = _run(tmp_path)
    second = _run(tmp_path)

    # assert
    assert first.returncode == 0 and second.returncode == 0
    assert link.is_symlink() and os.path.realpath(link) == str(SKILL_DIR.resolve())
    assert "already installed" in second.stdout
    assert _run(tmp_path, "--uninstall").returncode == 0 and not link.exists()
    assert _run(tmp_path, "--uninstall").returncode == 0


def test_install_codex_refuses_foreign_dir_then_force_backs_it_up(tmp_path):
    # arrange
    link = tmp_path / "skills" / "agent-board"
    link.mkdir(parents=True)
    (link / "mine.txt").write_text("x")

    # act
    refused = _run(tmp_path)
    refused_state = ((link / "mine.txt").is_file(), link.is_symlink())
    forced = _run(tmp_path, "--force")

    # assert
    assert refused.returncode == 1 and "--force" in refused.stderr
    assert refused_state == (True, False)
    assert forced.returncode == 0 and link.is_symlink()
    backups = list((tmp_path / "skills").glob("agent-board.bak.*"))
    assert len(backups) == 1 and (backups[0] / "mine.txt").read_text() == "x"
    assert "moved existing" in forced.stdout


def test_install_codex_force_replaces_foreign_symlink_without_touching_target(tmp_path):
    # arrange
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("k")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "agent-board").symlink_to(other)

    # act
    forced = _run(tmp_path, "--force")

    # assert
    assert forced.returncode == 0
    assert os.path.realpath(skills / "agent-board") == str(SKILL_DIR.resolve())
    assert (other / "keep.txt").read_text() == "k"


def test_readme_install_line__uses_marketplace_name():
    # arrange
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # act
    installs = re.findall(r"/plugin install (\S+)", readme)

    # assert
    assert installs and all(i == "agent-board@" + market["name"] for i in installs)


def test_install_codex_uninstall__foreign_symlink__refused_unless_forced(tmp_path):
    # arrange
    other = tmp_path / "other"
    other.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    link = skills / "agent-board"
    link.symlink_to(other)

    # act
    refused = _run(tmp_path, "--uninstall")
    kept = link.is_symlink()
    forced = _run(tmp_path, "--uninstall", "--force")

    # assert
    assert refused.returncode == 1 and "--force" in refused.stderr and kept
    assert forced.returncode == 0 and not link.is_symlink() and other.is_dir()
