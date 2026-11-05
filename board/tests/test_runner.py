from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_board import harness as harness_mod
from agent_board.board import new_task_from_dict
from agent_board.config import ProjectConfig
from agent_board.harness import FakeHarness
from agent_board.model import Status
from agent_board.server import BoardServer
from agent_board.store import ProjectStore
from agent_board.worktree import ensure_worktree, run_git

TIMEOUT = 10.0


def script(*steps) -> str:
    return "FAKE-SCRIPT: " + json.dumps(list(steps), ensure_ascii=False)


def wait_for(cond, timeout=TIMEOUT, what="условие"):
    deadline = time.monotonic() + timeout
    while True:
        value = cond()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("не дождались: %s" % what)
        time.sleep(0.05)


def events(store, type=None, key=None):
    return [
        e for e in store.read_events()
        if (type is None or e.type == type) and (key is None or e.data.get("key") == key)
    ]


def wait_events(store, type, key=None, count=1):
    return wait_for(lambda: len(events(store, type, key)) >= count and events(store, type, key),
                    what="%d событий %s %s" % (count, type, key or ""))


def task(store, key):
    return store.load_tasks()[key]


@pytest.fixture
def make_project(tmp_home, git_repo):
    def make(**kw):
        kw.setdefault("repos", [{"name": "app", "path": str(git_repo), "base": "main"}])
        kw.setdefault("harness", {"type": "fake"})
        store = ProjectStore("pay")
        store.create(ProjectConfig(slug="pay", title="Платежи", **kw))
        return store
    return make


@pytest.fixture
def start_server(tmp_home):
    servers = []

    def start():
        srv = BoardServer(port=0, runner=True)
        srv.start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.stop()


@pytest.fixture
def calls(monkeypatch):
    """Запоминает каждый запуск fake-агента: вид хода, сессию, текст, процесс."""
    recorded = []
    orig_start, orig_resume = FakeHarness.start_turn, FakeHarness.resume_turn

    def start_turn(self, cwd, prompt, session_id, log, env):
        proc = orig_start(self, cwd, prompt, session_id, log, env)
        recorded.append({"kind": "start", "cwd": Path(cwd), "text": prompt, "session": session_id, "env": env, "proc": proc})
        return proc

    def resume_turn(self, cwd, session_id, message, log, env):
        proc = orig_resume(self, cwd, session_id, message, log, env)
        recorded.append({"kind": "resume", "cwd": Path(cwd), "text": message, "session": session_id, "env": env, "proc": proc})
        return proc

    monkeypatch.setattr(FakeHarness, "start_turn", start_turn)
    monkeypatch.setattr(FakeHarness, "resume_turn", resume_turn)
    return recorded


def add(srv, key, description="", **extra):
    d = {"key": key, "title": "Задача " + key, "description": description}
    d.update(extra)
    return srv.board_for("pay").add_task(new_task_from_dict(d), "user")


def thread_texts(store, thread):
    return [m.text for m in store.read_thread(thread)]


# --- запуск задачи ---

def test_ready_task_starts_in_worktree_with_session(make_project, start_server, calls, git_repo, tmp_home):
    # arrange
    store = make_project()
    srv = start_server()
    # act
    add(srv, "T-1", script({"cli": ["status", "T-1", "in_progress"]}, {"write": {"path": "f.txt", "text": "x"}}))
    wait_events(store, "turn_finished", "T-1")
    # assert
    t = task(store, "T-1")
    wt = git_repo / ".agent-board-worktrees" / "T-1"
    assert (wt / "f.txt").read_text() == "x"
    assert (t.worktree, t.branch, t.base_branch, t.harness) == (str(wt), "T-1", "main", "fake")
    assert t.session_id and t.session_id == calls[0]["session"] and calls[0]["kind"] == "start"
    assert (t.agent_state, t.turn_error, t.status) == ("idle", "", Status.IN_PROGRESS)
    assert t.changed_files == ["f.txt"] and t.last_turn_at
    env = calls[0]["env"]
    bin_dir = store.root / "bin"
    assert env["AGENT_BOARD_TASK"] == "T-1" and env["AGENT_BOARD_PROJECT"] == "pay"
    assert env["AGENT_BOARD_ACTOR"] == "agent" and env["AGENT_BOARD_HOME"] == str(tmp_home)
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)
    assert (store.root / "logs" / "T-1.jsonl").is_file()
    snap = {x["key"]: x for x in srv.board_for("pay").snapshot()["tasks"]}
    assert t.session_id in snap["T-1"]["terminal_command"]


def test_board_cli_wrapper_runs_package(make_project, start_server):
    # arrange
    store = make_project()
    runner_sync(start_server())
    wrapper = store.root / "bin" / "agent-board"
    # act
    res = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, timeout=TIMEOUT,
                         env={"PATH": "/usr/bin:/bin"})
    # assert
    assert os.access(str(wrapper), os.X_OK)
    assert res.returncode == 0 and "lock-run" in res.stdout


def test_messages_during_turn_go_in_one_next_turn(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1", script({"sleep": 2}))
    wait_events(store, "turn_started", "T-1")
    # act
    board.post_message("T-1", "user", "первое")
    board.post_message("T-1", "user", "второе")
    wait_events(store, "turn_finished", "T-1", count=2)
    runner_sync(srv)
    # assert
    first_done = events(store, "turn_finished", "T-1")[0].seq
    assert all(e.seq < first_done for e in events(store, "message_posted"))
    assert len(events(store, "turn_started", "T-1")) == 2
    assert [c["kind"] for c in calls] == ["start", "resume"]
    resume = calls[1]
    assert resume["session"] == task(store, "T-1").session_id
    assert "первое" in resume["text"] and "второе" in resume["text"]
    assert resume["text"].index("первое") < resume["text"].index("второе")


def runner_sync(srv):
    assert srv.runner.sync(TIMEOUT)


def test_max_agents_limit_queues_second_task(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    # act
    add(srv, "A", script({"sleep": 1}))
    wait_events(store, "turn_started", "A")
    add(srv, "B")
    wait_for(lambda: task(store, "B").agent_state == "waiting_turn", what="B в очереди")
    wait_events(store, "turn_finished", "B")
    # assert
    a_done = events(store, "turn_finished", "A")[0].seq
    b_started = events(store, "turn_started", "B")[0].seq
    assert b_started > a_done
    assert task(store, "B").agent_state == "idle"


def test_failing_turn_sets_error_and_stops_after_three_failures(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    # act
    add(srv, "T-1", script({"fail": "всё сломалось"}))
    wait_events(store, "turn_failed", "T-1", count=3)
    wait_for(lambda: any("3" in t for t in thread_texts(store, "T-1")), what="системное сообщение")
    runner_sync(srv)
    # assert
    t = task(store, "T-1")
    assert (t.agent_state, t.turn_error) == ("error", "всё сломалось")
    assert len(events(store, "turn_started", "T-1")) == 3
    assert all(c["kind"] == "start" for c in calls)
    assert events(store, "turn_failed", "T-1")[0].data["error"] == "всё сломалось"


def test_user_message_after_failures_restarts_agent(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"fail": "boom"}))
    wait_events(store, "turn_failed", "T-1", count=3)
    runner_sync(srv)
    # act
    srv.board_for("pay").post_message("T-1", "user", "попробуй ещё")
    wait_events(store, "turn_started", "T-1", count=4)
    wait_events(store, "turn_failed", "T-1", count=4)
    # assert
    assert "попробуй ещё" in calls[3]["text"]


def test_missing_binary_reports_turn_failed(make_project, start_server, monkeypatch):
    # arrange
    monkeypatch.setattr(harness_mod.ClaudeHarness, "binary", "agent-board-no-such-binary")
    store = make_project(harness={"type": "claude", "permission_mode": "auto"})
    srv = start_server()
    # act
    add(srv, "T-1")
    failed = wait_events(store, "turn_failed", "T-1", count=3)
    runner_sync(srv)
    # assert
    assert "agent-board-no-such-binary" in failed[0].data["error"]
    assert "agent-board-no-such-binary" in task(store, "T-1").turn_error
    assert len(events(store, "turn_started", "T-1")) == 3


def test_approval_by_user_commits_and_dependent_starts_from_it(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "A", script({"write": {"path": "a-file.txt", "text": "from A"}}, {"cli": ["status", "A", "in_review"]}))
    add(srv, "B", deps=["A"])
    wait_events(store, "turn_finished", "A")
    # act
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    committed = wait_events(store, "task_committed", "A")
    wait_events(store, "turn_finished", "B")
    # assert
    sha = committed[0].data["sha"]
    assert run_git(git_repo, "log", "-1", "--format=%h %s", "A").strip() == "%s A Задача A" % sha
    b = task(store, "B")
    assert b.base_branch == "A"
    assert (Path(b.worktree) / "a-file.txt").read_text() == "from A"
    assert committed[0].seq < events(store, "turn_started", "B")[0].seq


def test_agent_stop_kills_turn_process(make_project, start_server, calls, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"sleep": 30}))
    wait_events(store, "turn_started", "T-1")
    proc = calls[0]["proc"]
    # act
    srv.board_for("pay").transition("T-1", Status.CANCELLED, "user")
    finished = wait_events(store, "turn_finished", "T-1")
    # assert
    assert proc.poll() is not None
    assert finished[0].data.get("stopped") is True
    assert task(store, "T-1").agent_state == "idle"
    assert not events(store, "turn_failed", "T-1")


def test_terminate_escalates_to_sigkill(tmp_path):
    # arrange
    proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 30"], start_new_session=True)
    # act
    started = time.monotonic()
    harness_mod.terminate(proc, grace=0.3)
    # assert
    assert proc.wait(timeout=TIMEOUT) != 0
    assert time.monotonic() - started < 5


def test_running_task_on_start_is_interrupted_and_resumed(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.status, t.session_id, t.worktree, t.branch, t.base_branch = Status.IN_PROGRESS, "s-1", str(wt), "T-1", "main"
    t.agent_state, t.harness = "running", "fake"
    store.save_task(t)
    # act
    srv = start_server()
    wait_events(store, "turn_finished", "T-1")
    # assert
    wake = [e for e in events(store, "agent_wake", "T-1") if e.data["reason"] == "interrupted"]
    assert len(wake) == 1
    assert any("прерван" in text for text in thread_texts(store, "T-1"))
    assert [(c["kind"], c["session"]) for c in calls] == [("resume", "s-1")]
    assert "interrupted" in calls[0]["text"]
    assert task(store, "T-1").session_id == "s-1"


def test_escalation_wakes_project_agent_with_digest(make_project, start_server, calls):
    # arrange
    store = make_project(project_session_id="p-1")
    srv = start_server()
    # act
    add(srv, "T-1", script({"cli": ["say", "_project", "--text", "нужна помощь"]}))
    wait_events(store, "turn_finished", "_project")
    # assert
    project_calls = [c for c in calls if c["env"]["AGENT_BOARD_ACTOR"] == "project_agent"]
    assert [(c["kind"], c["session"]) for c in project_calls] == [("resume", "p-1")]
    text = project_calls[0]["text"]
    assert "Escalation from task agent T-1" in text and "нужна помощь" in text
    assert "## Board changes since your last turn" in text and "T-1: task added" in text
    assert project_calls[0]["cwd"] == store.root / "project-agent"
    assert not project_calls[0]["env"].get("AGENT_BOARD_TASK")
    assert store.load_meta()["project_last_seen"] > 0


def test_first_project_wake_starts_session_with_project_prompt(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    # act
    srv.board_for("pay").post_message("_project", "user", "привет, агент проекта")
    wait_events(store, "turn_finished", "_project")
    # assert
    assert [c["kind"] for c in calls] == ["start"]
    assert "You are the project agent" in calls[0]["text"] and "привет, агент проекта" in calls[0]["text"]
    assert store.load_config().project_session_id == calls[0]["session"]
    assert store.load_meta()["project_agent_state"] == "idle"


def test_work_stage_starts_project_agent_on_runner_start(make_project, start_server, calls):
    # arrange
    store = make_project(stage="work")
    # act
    start_server()
    wait_events(store, "turn_finished", "_project")
    # assert
    assert [c["kind"] for c in calls] == ["start"]
    assert store.load_config().project_session_id == calls[0]["session"]


def test_reapproved_dependency_rebases_dependent_carrying_its_changes(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "A", script({"write": {"path": "a.txt", "text": "A v1\n"}}))
    add(srv, "B", script({"write": {"path": "b.txt", "text": "B work\n"}}), deps=["A"])
    wait_events(store, "turn_finished", "A")
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "B")
    (Path(task(store, "A").worktree) / "a2.txt").write_text("A v2\n")
    # act
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "B", count=2)
    # assert
    b_wt = Path(task(store, "B").worktree)
    assert (b_wt / "a2.txt").read_text() == "A v2\n"
    assert (b_wt / "b.txt").read_text() == "B work\n"
    assert run_git(b_wt, "rev-parse", "HEAD") == run_git(git_repo, "rev-parse", "A")
    resume = [c for c in calls if c["kind"] == "resume" and c["session"] == task(store, "B").session_id]
    assert "the board moved your work onto A" in resume[-1]["text"]


def test_task_without_resolvable_repo_gets_error(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project(repos=[{"name": "one", "path": str(git_repo)}, {"name": "two", "path": str(git_repo)}])
    srv = start_server()
    # act
    add(srv, "T-1")
    wait_for(lambda: task(store, "T-1").agent_state == "error", what="ошибка репозитория")
    runner_sync(srv)
    # assert
    assert "репозитор" in task(store, "T-1").turn_error
    assert any("репозитор" in text for text in thread_texts(store, "T-1"))
    assert calls == []


def test_blocked_dependent_defers_rebase_until_unblocked(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    a_wt = ensure_worktree(git_repo, "A", "A", "main", [])
    (a_wt / "a.txt").write_text("A\n")
    run_git(a_wt, "commit", "-am", "A")
    b_wt = ensure_worktree(git_repo, "B", "B", "A", [])
    store.save_task(new_task_from_dict({"key": "C", "title": "C"}))
    a = new_task_from_dict({"key": "A", "title": "A"})
    a.status, a.worktree, a.branch = Status.APPROVED, str(a_wt), "A"
    store.save_task(a)
    b = new_task_from_dict({"key": "B", "title": "B", "deps": ["A", "C"]})
    b.status, b.session_id, b.worktree, b.branch, b.base_branch, b.harness = (
        Status.IN_PROGRESS, "s-b", str(b_wt), "B", "A", "fake")
    store.save_task(b)
    srv = start_server()
    board = srv.board_for("pay")
    (a_wt / "a2.txt").write_text("A2\n")
    # act
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "task_committed", "A")
    runner_sync(srv)
    deferred = list(events(store, "turn_started", "B"))
    board.transition("C", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "B")
    # assert
    assert deferred == []
    assert [(c["kind"], c["session"]) for c in calls] == [("resume", "s-b")]
    assert calls[0]["text"].count("the board moved your work onto A") == 1
    assert (b_wt / "a2.txt").read_text() == "A2\n"
