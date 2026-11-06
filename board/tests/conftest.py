from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board.model import Task
from agent_board.store import ProjectStore

NOW = "2026-01-01T00:00:00+00:00"


def _home_fingerprint(root: Path):
    """Существование и времена изменения всего дерева каталога (None — каталога нет)."""
    if not root.exists():
        return None
    items = [(".", root.stat().st_mtime_ns)]
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            try:
                items.append((str(path.relative_to(root)), path.lstat().st_mtime_ns))
            except OSError:
                pass
    return sorted(items)


@pytest.fixture(scope="session", autouse=True)
def real_board_home_untouched():
    """Тесты не должны трогать настоящий ~/.agent-board (например, после снятия AGENT_BOARD_HOME)."""
    root = Path(os.path.expanduser("~")) / ".agent-board"
    before = _home_fingerprint(root)
    yield
    after = _home_fingerprint(root)
    if after != before:
        pytest.fail("Тесты изменили настоящий %s: было %r, стало %r" % (root, before, after), pytrace=False)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_HOME", str(tmp_path))
    return tmp_path


def make_task(key, **kw) -> Task:
    kw.setdefault("title", key)
    return Task(key=key, **kw)


def make_board(chains, on_event=None):
    s = ProjectStore("pay")
    s.create(ProjectConfig(slug="pay", title="Платежи", chains=chains))
    return Board(s, clock=lambda: NOW, on_event=on_event)


@pytest.fixture
def board(tmp_home):
    return make_board(True)


@pytest.fixture
def board_strict(tmp_home):
    return make_board(False)


def flat(events):
    return [dict(type=e.type, **e.data) for e in events]


def wake(key, reason, **extra):
    return dict(type="agent_wake", key=key, reason=reason, **extra)


@pytest.fixture
def git_repo(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "Tester")
    git("config", "user.email", "tester@example.com")
    (repo / "a.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-m", "init")
    return repo
