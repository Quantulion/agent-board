#!/usr/bin/env python3
"""Создаёт изолированное окружение сценария: <dir>/home (AGENT_BOARD_HOME) и <dir>/repo (git-репозиторий).

Использование: make_env.py <dir> [--existing-project]
Печатает export-строки для shell. Настоящий ~/.agent-board не трогается.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEMO_SLUG, ROOT, WRAPPER, cli, cli_json, git_env  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

APP = '''def add_item(items, text):
    """Добавляет пункт в список и возвращает новый список."""
    return items + [text]
'''
TEST = '''from app import add_item


def test_add_item():
    assert add_item([], "a") == ["a"]
'''


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True, text=True,
                   env=git_env())


def make_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Scenario User")
    _git(repo, "config", "user.email", "scenario@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text(APP, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(TEST, encoding="utf-8")
    (repo / "README.md").write_text("# Tiny project\n", encoding="utf-8")
    (repo / "docs").mkdir()
    shutil.copy(str(FIXTURES / "plan.md"), str(repo / "docs" / "plan.md"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial commit")


def make_existing_project(env_dir: Path, repo: Path) -> None:
    """Проект demo с репозиторием и одной задачей; сервер после этого остановлен. Эталон — baseline.json."""
    cli(env_dir, ["init", DEMO_SLUG, "--title", "Demo"])
    cli(env_dir, ["serve", "--detach"])
    try:
        cfg = {"repos": [{"name": "repo", "path": str(repo), "base": "main"}], "harness": {"type": "fake"},
               "review": {"tool": "none"}}
        cli(env_dir, ["--project", DEMO_SLUG, "config", "set", "--json", json.dumps(cfg)])
        task = {"key": "DEMO-1", "title": "Existing task", "description": "Already there",
                "done_when": "Tests pass", "repo": "repo", "kind": "code", "deps": []}
        cli(env_dir, ["--project", DEMO_SLUG, "add-task", "--json", json.dumps(task)])
        baseline = project_fingerprint(env_dir, DEMO_SLUG)
    finally:
        cli(env_dir, ["stop"], check=False)
    (env_dir / "baseline.json").write_text(json.dumps(baseline, indent=1, sort_keys=True), encoding="utf-8")


def project_fingerprint(env_dir: Path, slug: str) -> dict:
    state = cli_json(env_dir, ["--project", slug, "state"])
    config = dict(state["config"])
    return {"config": config, "tasks": [{k: t[k] for k in ("key", "title", "status", "deps", "repo")}
                                         for t in state["tasks"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dir")
    parser.add_argument("--existing-project", action="store_true", help="создать проект demo (сценарий 3)")
    args = parser.parse_args(argv)
    env_dir = Path(args.dir).resolve()
    if env_dir.exists() and any(env_dir.iterdir()):
        parser.error("каталог %s не пуст" % env_dir)
    (env_dir / "home").mkdir(parents=True)
    make_repo(env_dir / "repo")
    if args.existing_project:
        make_existing_project(env_dir, env_dir / "repo")
    print("export AGENT_BOARD_HOME=%s" % (env_dir / "home"))
    print("export AGENT_BOARD_PYTHON=%s" % sys.executable)
    print("export AGENT_BOARD_REPO=%s" % (env_dir / "repo"))
    print("unset AGENT_BOARD_PROJECT AGENT_BOARD_TASK AGENT_BOARD_ACTOR")
    print("# wrapper: %s" % WRAPPER)
    print("# skill: %s" % (ROOT / "skills" / "agent-board" / "SKILL.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
