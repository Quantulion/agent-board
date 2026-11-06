from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from test_publishability import internal_name_hits

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export-repo.py"


def _export(source: Path, dest: Path, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), str(dest), "--source", str(source),
                           "--author-name", "Jane Doe", "--author-email", "jane@example.com", *extra],
                          capture_output=True, text=True, timeout=120)


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout


def test_export__repo_with_history__single_commit_by_given_author(git_repo, tmp_path):
    # arrange
    (git_repo / "b.txt").write_text("second\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "second")
    dest = tmp_path / "out"

    # act
    r = _export(git_repo, dest, "--message", "Hello")

    # assert
    assert r.returncode == 0, r.stderr
    assert _git(dest, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(dest, "log", "-1", "--format=%an|%ae|%cn|%ce|%s").strip() == \
        "Jane Doe|jane@example.com|Jane Doe|jane@example.com|Hello"
    assert _git(dest, "branch", "--show-current").strip() == "main"
    assert _git(dest, "status", "--porcelain") == ""


def test_export__ignored_superpowers_and_uncommitted__takes_current_tree_only(git_repo, tmp_path):
    # arrange
    (git_repo / ".gitignore").write_text("secret.txt\n")
    (git_repo / "secret.txt").write_text("ignored\n")
    (git_repo / ".superpowers").mkdir()
    (git_repo / ".superpowers" / "notes.md").write_text("draft\n")
    (git_repo / "a.txt").write_text("edited\n")
    (git_repo / "new.txt").write_text("untracked\n")
    dest = tmp_path / "out"

    # act
    r = _export(git_repo, dest)

    # assert
    assert r.returncode == 0, r.stderr
    assert sorted(_git(dest, "ls-files").split()) == [".gitignore", "a.txt", "new.txt"]
    assert (dest / "a.txt").read_text() == "edited\n"


def test_export__dest_not_empty__fails_without_changes(git_repo, tmp_path):
    # arrange
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("x")

    # act
    r = _export(git_repo, dest)

    # assert
    assert r.returncode != 0
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]


def test_export__this_repo__result_is_publishable(tmp_path):
    # arrange
    dest = tmp_path / "out"

    # act
    r = _export(ROOT, dest)

    # assert
    assert r.returncode == 0, r.stderr
    assert not (dest / ".superpowers").exists()
    assert internal_name_hits(dest) == []


def test_plugin_metadata__author_homepage_repository_filled():
    # arrange
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    # act
    entry = market["plugins"][0]

    # assert
    assert plugin["author"]["name"] and plugin["author"]["url"].startswith("https://github.com/")
    assert plugin["repository"].startswith(plugin["author"]["url"] + "/")
    assert plugin["homepage"].startswith(plugin["repository"])
    assert market["owner"] == plugin["author"] == entry["author"]
    assert (entry["homepage"], entry["repository"]) == (plugin["homepage"], plugin["repository"])
    assert entry["version"] == plugin["version"]
