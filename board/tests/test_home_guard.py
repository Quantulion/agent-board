from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import home_guard
from agent_board.cli import main
from agent_board.harness import FakeHarness
from agent_board.server import BoardServer
from agent_board.store import board_home

TESTS_DIR = Path(__file__).parent
REAL_HOME = Path(os.path.expanduser("~")) / ".agent-board"


def make_live_home(root: Path) -> Path:
    """Каталог, устроенный как дом работающей доски с одним проектом."""
    project = root / "projects" / "live"
    (project / "logs").mkdir(parents=True)
    (project / "tasks").mkdir()
    (project / "project.json").write_text('{"slug": "live"}')
    (project / "events.jsonl").write_text("")
    (root / "server.json").write_text('{"port": 1}')
    (root / "token").write_text("secret")
    return root


def run_inner_session(tmp_path: Path, user_home: Path, test_body: str) -> subprocess.CompletedProcess:
    """Отдельный прогон pytest с нашим conftest, будто запущенный из сессии агента на машине с доской."""
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy(str(TESTS_DIR / "conftest.py"), str(suite / "conftest.py"))
    shutil.copy(str(TESTS_DIR / "home_guard.py"), str(suite / "home_guard.py"))
    (suite / "test_inner.py").write_text(textwrap.dedent(test_body))
    env = dict(
        os.environ,
        HOME=str(user_home),
        AGENT_BOARD_HOME=str(user_home / ".agent-board"),
        AGENT_BOARD_PROJECT="live",
        AGENT_BOARD_TASK="LIVE-1",
        AGENT_BOARD_ACTOR="agent",
        PYTHONPATH=os.pathsep.join(filter(None, [str(TESTS_DIR.parent), os.environ.get("PYTHONPATH")])),
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suite)],
        cwd=str(suite), env=env, capture_output=True, text=True, timeout=120,
    )


def test_changes__live_agents_write_logs_tasks_events__should_report_nothing(tmp_path):
    # arrange
    root = make_live_home(tmp_path / "home")
    before = home_guard.snapshot(root)
    project = root / "projects" / "live"
    # act
    (project / "logs" / "LIVE-1.jsonl").write_text("{}")
    (project / "tasks" / "LIVE-1.json").write_text("{}")
    (project / "events.jsonl").write_text("{}\n")
    (root / "server.log").write_text("started")
    # assert
    assert home_guard.changes(before, home_guard.snapshot(root)) == []


def test_changes__board_settings_changed__should_report_each_path(tmp_path):
    # arrange
    root = make_live_home(tmp_path / "home")
    before = home_guard.snapshot(root)
    # act
    (root / "projects" / "pay").mkdir()
    (root / "server.json").write_text('{"port": 2}')
    (root / "token").write_text("other")
    (root / "projects" / "live" / "project.json").write_text('{"slug": "live", "title": "x"}')
    # assert
    assert home_guard.changes(before, home_guard.snapshot(root)) == [
        "projects/live/project.json", "projects/pay/project.json", "server.json", "token",
    ]


@pytest.mark.parametrize("touch", [
    "(REAL / 'projects' / 'pay').mkdir()",
    "(REAL / 'server.json').write_text('{\"port\": 2}')",
])
def test_session_guard__test_touches_real_home__should_fail_run(tmp_path, touch):
    # arrange
    user_home = tmp_path / "user"
    make_live_home(user_home / ".agent-board")
    body = """
        import os
        from pathlib import Path

        REAL = Path(os.path.expanduser("~")) / ".agent-board"

        def test_touch():
            %s
    """ % touch
    # act
    result = run_inner_session(tmp_path, user_home, body)
    # assert
    assert result.returncode != 0
    assert "Тесты изменили настоящий" in result.stdout


def test_session_guard__agent_session_env_and_live_agents__should_isolate_and_pass(tmp_path):
    # arrange
    user_home = tmp_path / "user"
    real = make_live_home(user_home / ".agent-board")
    body = """
        import os
        from pathlib import Path

        from agent_board.store import board_home

        REAL = Path(os.path.expanduser("~")) / ".agent-board"

        def test_isolated():
            assert sorted(n for n in os.environ if n.startswith("AGENT_BOARD_")) == ["AGENT_BOARD_HOME"]
            assert board_home() != REAL
            # живой агент тем временем пишет журнал и события
            (REAL / "projects" / "live" / "logs" / "LIVE-1.jsonl").write_text("{}")
            (REAL / "projects" / "live" / "events.jsonl").write_text("{}\\n")
    """
    # act
    result = run_inner_session(tmp_path, user_home, body)
    # assert
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (real / "projects").iterdir()) == ["live"]


def test_isolated_env__board_home_unset__should_point_to_temporary_dir(monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_HOME")
    # act
    home = board_home()
    # assert
    assert home != REAL_HOME
    assert REAL_HOME not in home.parents


@pytest.fixture
def srv():
    assert main(["init", "pay", "--title", "Платежи"]) == 0
    server = BoardServer(port=0)
    server.start()
    yield server
    server.stop()


def test_isolated_env__child_cli_without_explicit_home__should_use_temporary_board(srv, tmp_path):
    # arrange
    assert main(["--project", "pay", "add-task", "--json", '{"key": "T1", "title": "x"}']) == 0
    prompt = "FAKE-SCRIPT: " + json.dumps([{"cli": ["--project", "pay", "status", "T1", "in_progress"]}])
    h = FakeHarness()
    log = tmp_path / "l"
    # act
    res = h.finish(h.start_turn(tmp_path, prompt, None, log, {}), log)
    # assert
    assert res.ok is True, res.error
    assert srv.hub.board("pay").snapshot()["tasks"][0]["status"] == "in_progress"
    assert REAL_HOME not in board_home().parents and board_home() != REAL_HOME
