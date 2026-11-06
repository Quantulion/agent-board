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
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)


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


def test_install_codex_refuses_foreign_path_unless_forced(tmp_path):
    # arrange
    link = tmp_path / "skills" / "agent-board"
    link.mkdir(parents=True)
    (link / "mine.txt").write_text("x")

    # act
    refused = _run(tmp_path)
    forced = _run(tmp_path, "--force")

    # assert
    assert refused.returncode == 1 and "--force" in refused.stderr
    assert (link / "mine.txt").exists() is False or link.is_symlink()
    assert forced.returncode == 0 and link.is_symlink()
