from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from agent_board.config import ProjectConfig
from agent_board.harness import group_alive
from agent_board.runner import LEFTOVER_MISSING_TEXT
from agent_board.server import read_discovery
from agent_board.store import ProjectStore
from agent_board.worktree import run_git

from test_runner import script, wait_events, wait_for

PKG_ROOT = str(Path(__file__).resolve().parent.parent)
WAIT = 20.0


class ServerProcess:
    """Доска в отдельном процессе: `python -m agent_board serve` с временным AGENT_BOARD_HOME."""

    def __init__(self, home: Path):
        self.home = home
        self.proc = None
        self.launches = 0

    def start(self) -> None:
        env = dict(os.environ, AGENT_BOARD_HOME=str(self.home))
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [PKG_ROOT, os.environ.get("PYTHONPATH")]))
        self.launches += 1
        out = open(str(self.home / ("server-%d.log" % self.launches)), "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "agent_board", "serve", "--port", "0"],
                                     env=env, stdout=out, stderr=subprocess.STDOUT)
        out.close()
        wait_for(lambda: (read_discovery() or {}).get("pid") == self.proc.pid, WAIT, "discovery нового сервера")

    def kill(self) -> None:
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=WAIT)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=WAIT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def call(self, method: str, path: str, body=None):
        info = read_discovery()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d/api/p/pay/%s" % (info["port"], path), data=data, method=method,
                                     headers={"X-Board-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))


def fake_pgids(store):
    return [t.turn_process["pgid"] for t in store.load_tasks().values() if t.turn_process]


def test_full_flow_with_server_crash_during_dependent_turn(tmp_home, git_repo):
    # arrange
    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи", harness={"type": "fake"},
                               repos=[{"name": "app", "path": str(git_repo), "base": "main"}]))
    server = ServerProcess(tmp_home)
    old_group = None
    try:
        server.start()
        server.call("POST", "stage", {"stage": "work"})
        a_script = script({"cli": ["status", "A", "in_progress"]}, {"write": {"path": "a-file.txt", "text": "from A"}},
                          {"cli": ["ask", "A", "--text", "Продолжать?", "--option", "да"]})
        b_script = script({"cli": ["status", "B", "in_progress"]}, {"write": {"path": "b-started.txt", "text": "b"}},
                          {"sleep": 120})
        server.call("POST", "tasks", {"actor": "user", "task": {"key": "A", "title": "Задача A", "description": a_script}})
        server.call("POST", "tasks", {"actor": "user", "task": {"key": "B", "title": "Задача B", "description": b_script,
                                                                "deps": ["A"]}})
        # act: A задаёт вопрос, пользователь отвечает, агент отправляет задачу в ревью
        wait_events(store, "question_asked", "A")
        question = store.load_tasks()["A"].questions[0]
        wait_events(store, "turn_finished", "A")
        resume_a = script({"cli": ["status", "A", "in_review"]})
        server.call("POST", "tasks/A/questions/%s/answer" % question.id, {"choice": "да", "comment": resume_a})
        wait_for(lambda: store.load_tasks()["A"].status.value == "in_review", WAIT, "A в in_review")
        wait_events(store, "turn_finished", "A", count=2)
        server.call("POST", "tasks/A/transition", {"to": "approved", "actor": "user"})
        committed = wait_events(store, "task_committed", "A")
        wait_events(store, "turn_started", "B")
        wait_for(lambda: (Path(store.load_tasks()["B"].worktree) / "b-started.txt").exists(), WAIT, "B дошла до sleep")
        # assert: коммит A, код A в worktree B
        b = store.load_tasks()["B"]
        assert run_git(git_repo, "log", "-1", "--format=%h %s", "A").strip() == "%s A Задача A" % committed[0].data["sha"]
        assert b.base_branch == "A"
        assert (Path(b.worktree) / "a-file.txt").read_text() == "from A"
        a_events = [e.type for e in store.read_events() if e.data.get("key") == "A" and e.type.startswith("turn_")]
        assert a_events == ["turn_started", "turn_finished", "turn_started", "turn_finished"]
        session_before, old_group = b.session_id, b.turn_process["pgid"]
        assert b.agent_state == "running" and group_alive(old_group)

        # act: сервер падает посреди хода B и поднимается заново
        server.kill()
        assert group_alive(old_group)
        server.start()
        wait_events(store, "turn_finished", "B")
        # assert
        b = store.load_tasks()["B"]
        wake = [e for e in store.read_events() if e.type == "agent_wake" and e.data.get("key") == "B"]
        assert [e.data["reason"] for e in wake if e.data["reason"] == "interrupted"] == ["interrupted"]
        assert b.session_id == session_before
        assert b.agent_state == "idle" and not b.turn_process
        wait_for(lambda: not group_alive(old_group), WAIT, "старый процесс B остановлен")
        texts = [m.text for m in store.read_thread("B")]
        assert any("прерван" in t for t in texts) and LEFTOVER_MISSING_TEXT not in texts
        assert len([e for e in store.read_events() if e.type == "turn_started" and e.data.get("key") == "B"]) == 2
        assert not [e for e in store.read_events() if e.type == "turn_failed"]
    except BaseException:
        for log in sorted(tmp_home.glob("server-*.log")):
            print("--- %s ---\n%s" % (log.name, log.read_text()))
        raise
    finally:
        server.stop()
        for pgid in fake_pgids(store) + ([old_group] if old_group else []):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
