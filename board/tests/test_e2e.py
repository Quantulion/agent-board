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

from fakes import fake_crit
from fakes.fake_jira import FakeJira

from agent_board.config import ProjectConfig
from agent_board.harness import group_alive
from agent_board.messages import CATALOG
from agent_board.server import read_discovery
from agent_board.store import ProjectStore
from agent_board.worktree import run_git

from test_runner import script, wait_events, wait_for

pytestmark = pytest.mark.slow

PKG_ROOT = str(Path(__file__).resolve().parent.parent)
WAIT = 20.0


class ServerProcess:
    """Доска в отдельном процессе: `python -m agent_board serve` с временным AGENT_BOARD_HOME."""

    def __init__(self, home: Path, extra_env=None, extra_args=()):
        self.home = home
        self.extra_env = dict(extra_env or {})
        self.extra_args = list(extra_args)
        self.proc = None
        self.launches = 0

    def start(self) -> None:
        env = dict(os.environ, AGENT_BOARD_HOME=str(self.home), **self.extra_env)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [PKG_ROOT, os.environ.get("PYTHONPATH")]))
        self.launches += 1
        out = open(str(self.home / ("server-%d.log" % self.launches)), "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "agent_board", "serve", "--port", "0"] + self.extra_args,
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
        server.call("POST", "stage", {"stage": "work", "actor": "user"})
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
        assert any("прерван" in t for t in texts) and CATALOG["run.leftover_missing"][1] not in texts
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


def test_review_approval_ship_and_tracker_sync_release_dependent_task(tmp_home, git_repo, tmp_path):
    # arrange: цепочки выключены — B ждёт, пока A станет done (по статусу в трекере)
    token = "e2e-secret-token"
    jira = FakeJira(project="PROJ", authorization="Bearer " + token).start()
    crit = fake_crit.install(tmp_path / "crit-bin")
    marker = tmp_path / "shipped.txt"
    store = ProjectStore("pay")
    store.create(ProjectConfig(
        slug="pay", title="Платежи", harness={"type": "fake"}, chains=False,
        repos=[{"name": "app", "path": str(git_repo), "base": "main"}],
        review={"tool": "crit", "binary": str(crit)},
        ship={"command": "printf '%s %s\\n' {key} {base} >> " + str(marker), "timeout": 60},
        tracker={"type": "jira", "url": jira.url, "project": "PROJ", "issue_type": "Task", "poll_seconds": 3600,
                 "auth": {"type": "token_env", "env": "E2E_JIRA_TOKEN"},
                 "statuses": {"Open": None, "Done": "done"}}))
    server = ServerProcess(tmp_home, {"E2E_JIRA_TOKEN": token}, ["--no-tracker-sync"])
    crit_dir = store.root / "crit"

    def review():
        return store.load_tasks()["A"].review or {}

    def round_open(n):
        r = review()
        return r.get("state") == "open" and r.get("round") == n and r.get("url") and r

    try:
        server.start()
        a_script = script({"write": {"path": "a-file.txt", "text": "from A"}}, {"cli": ["status", "A", "in_review"]})
        b_script = script({"write": {"path": "b-started.txt", "text": "b"}}, {"cli": ["status", "B", "in_review"]})
        server.call("POST", "tasks", {"actor": "user", "task": {"key": "A", "title": "Задача A", "description": a_script}})
        server.call("POST", "tasks", {"actor": "user", "task": {"key": "B", "title": "Задача B", "description": b_script,
                                                                "deps": ["A"]}})
        # act: заводим задачи в трекере и начинаем работу
        server.call("POST", "stage", {"stage": "tracker", "actor": "user"})
        created = server.call("POST", "tracker/create", {"actor": "user"})
        assert not created["errors"] and sorted(c["tracker_key"] for c in created["created"]) == ["PROJ-1", "PROJ-2"]
        server.call("POST", "stage", {"stage": "work", "actor": "user"})
        wait_for(lambda: store.load_tasks()["A"].status.value == "in_review"
                 and store.load_tasks()["A"].agent_state == "idle", WAIT, "A в in_review")

        # act: раунд 1 — комментарий, агент правит, раунд 2 — без комментариев
        server.call("POST", "tasks/A/review", {"actor": "user"})
        first = wait_for(lambda: round_open(1), WAIT, "раунд 1")
        assert first["url"].startswith("http://localhost:")
        fix = script({"write": {"path": "fix.txt", "text": "fixed"}})
        fake_crit.finish(crit_dir, first["session"], [{"path": "a-file.txt", "line": 1, "body": fix}])
        second = wait_for(lambda: round_open(2), WAIT, "раунд 2")
        assert second["url"] == first["url"] and (Path(store.load_tasks()["A"].worktree) / "fix.txt").exists()
        # поддельный crit не умеет «resolve» комментария агентом, поэтому одобрение — явное
        fake_crit.finish(crit_dir, second["session"], approved=True)
        committed = wait_events(store, "task_committed", "A")
        # assert: одобрение поставил пользователь, коммит на месте, B ещё не стартовала
        a = store.load_tasks()["A"]
        approved = [e.data for e in store.read_events()
                    if e.type == "status_changed" and e.data["key"] == "A" and e.data["to"] == "approved"]
        assert a.status.value == "approved" and approved[0]["actor"] == "user"
        assert [(h["round"], h["state"]) for h in a.review["history"]] == [(1, "changes"), (2, "approved")]
        assert run_git(git_repo, "log", "-1", "--format=%h", "A").strip() == committed[0].data["sha"]
        assert not [e for e in store.read_events() if e.type == "turn_started" and e.data.get("key") == "B"]

        # act: отправка командой проекта
        server.call("POST", "ship", {"keys": ["A"], "actor": "user"})
        # событие о завершении пишется последним: после него карточка уже в итоговом состоянии
        finished = wait_events(store, "ship_finished", "A")
        # assert
        assert [e.data["ok"] for e in finished] == [True]
        assert marker.read_text().splitlines() == ["A main"]
        a = store.load_tasks()["A"]
        assert a.status.value == "sent" and a.ship["state"] == "done"

        # act: первое чтение статусов, затем в Jira «Done»
        server.call("POST", "tracker/sync", {"actor": "user"})
        assert store.load_tasks()["A"].status.value == "sent"
        jira.statuses["PROJ-1"] = "Done"
        sync = server.call("POST", "tracker/sync", {"actor": "user"})
        wait_events(store, "turn_started", "B")
        wait_for(lambda: (Path(store.load_tasks()["B"].worktree) / "b-started.txt").exists(), WAIT, "B стартовала")
        # assert: A закрыта трекером, B стартовала от базы репозитория
        assert sync.get("ok") is True and not sync.get("paused")
        assert store.load_tasks()["A"].status.value == "done"
        changed = [e.data for e in store.read_events()
                   if e.type == "status_changed" and e.data["key"] == "A" and e.data["to"] == "done"]
        assert changed[0]["actor"] == "tracker"
        b = store.load_tasks()["B"]
        assert b.base_branch == "main" and not (Path(b.worktree) / "a-file.txt").exists()
        assert not [e for e in store.read_events() if e.type == "turn_failed"]
        assert token not in "".join(p.read_text() for p in tmp_home.rglob("*.json*") if p.is_file())
    except BaseException:
        for log in sorted(tmp_home.glob("server-*.log")):
            print("--- %s ---\n%s" % (log.name, log.read_text()))
        raise
    finally:
        server.stop()
        jira.stop()
        fake_crit.cleanup(tmp_path / "crit-bin")
        for pgid in fake_pgids(store):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
