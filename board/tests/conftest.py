from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import home_guard
import pytest
from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board.model import Task
from agent_board.store import ProjectStore

NOW = "2026-01-01T00:00:00+00:00"
REAL_HOME = Path(os.path.expanduser("~")) / ".agent-board"

# Переменные доски из сессии агента не должны доходить ни до тестов, ни до их дочерних процессов,
# а доска по умолчанию живёт во временном каталоге — ещё до сбора тестов и сессионных фикстур.
home_guard.clear_board_env()
os.environ["AGENT_BOARD_HOME"] = tempfile.mkdtemp(prefix="agent-board-home-")
atexit.register(shutil.rmtree, os.environ["AGENT_BOARD_HOME"], True)


@pytest.fixture(scope="session", autouse=True)
def real_board_home_untouched():
    """Проверка в конце прогона: тесты не создали проектов и не меняли настройки настоящей доски."""
    before = home_guard.snapshot(REAL_HOME)
    yield
    changed = home_guard.changes(before, home_guard.snapshot(REAL_HOME))
    if changed:
        pytest.fail("Тесты изменили настоящий %s: %s" % (REAL_HOME, ", ".join(changed)), pytrace=False)


@pytest.fixture(autouse=True)
def isolated_board_env(tmp_path_factory, monkeypatch):
    """Каждый тест начинает без переменных доски, с отдельным временным домом доски.

    Даже без AGENT_BOARD_HOME домашний каталог процесса указывает во временный каталог."""
    for name in [n for n in os.environ if n.startswith(home_guard.ENV_PREFIX)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AGENT_BOARD_HOME", str(tmp_path_factory.mktemp("board-home")))
    fake_user_home = tmp_path_factory.mktemp("user-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_user_home))


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
