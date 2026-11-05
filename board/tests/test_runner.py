from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_board import harness as harness_mod
from agent_board import runner as runner_mod
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
        if "stage" not in kw:
            # агенты задач работают только на стадии work; с готовой сессией агент проекта не стартует сам
            kw.update(stage="work")
            kw.setdefault("project_session_id", "p-0")
        store = ProjectStore("pay")
        store.create(ProjectConfig(slug="pay", title="Платежи", **kw))
        return store
    return make


@pytest.fixture(autouse=True)
def fast_runner(monkeypatch):
    monkeypatch.setattr(runner_mod, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(runner_mod, "STOP_GRACE_SECONDS", 2.0)


@pytest.fixture
def start_server(tmp_home):
    servers = []

    def start():
        srv = BoardServer(port=0, runner=True)
        srv.start()
        servers.append((srv, srv.runner))
        return srv

    yield start
    for srv, runner in servers:
        srv.stop()
        runner.kill_all_for_tests()


@pytest.fixture
def calls(monkeypatch):
    """Запоминает каждый запуск fake-агента: вид хода, сессию, текст, процесс."""
    recorded = []
    orig_start, orig_resume = FakeHarness.start_turn, FakeHarness.resume_turn

    def record(kind, cwd, text, session_id, env, proc, system):
        # какие из ранее запущенных групп процессов ещё живы в момент нового запуска
        alive = [c["proc"].pid for c in recorded if harness_mod.group_alive(c["proc"].pid)]
        recorded.append({"kind": kind, "cwd": Path(cwd), "text": text, "session": session_id, "env": env,
                         "proc": proc, "alive_before": alive, "system": system})

    def start_turn(self, cwd, prompt, session_id, log, env):
        proc = orig_start(self, cwd, prompt, session_id, log, env)
        record("start", cwd, prompt, session_id, env, proc, self.system_prompt)
        return proc

    def resume_turn(self, cwd, session_id, message, log, env):
        proc = orig_resume(self, cwd, session_id, message, log, env)
        record("resume", cwd, message, session_id, env, proc, self.system_prompt)
        return proc

    monkeypatch.setattr(FakeHarness, "start_turn", start_turn)
    monkeypatch.setattr(FakeHarness, "resume_turn", resume_turn)
    yield recorded
    for c in recorded:
        if c["proc"].poll() is None:
            harness_mod.terminate(c["proc"], 1.0)


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


def test_resume_turn_gets_task_system_prompt_from_current_config(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1")
    wait_events(store, "turn_finished", "T-1")
    board.update_config({"language": "эсперанто"})
    # act
    board.post_message("T-1", "user", "продолжай")
    wait_events(store, "turn_finished", "T-1", count=2)
    # assert
    assert calls[1]["kind"] == "resume"
    assert "You are a task agent" in calls[1]["system"] and "эсперанто" in calls[1]["system"]


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


def test_approval_during_turn_stops_turn_then_commits(make_project, start_server, calls, git_repo, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    seen = []
    orig_commit = runner_mod.commit_all

    def commit_all(path, message):
        seen.append(calls[0]["proc"].poll())
        return orig_commit(path, message)

    monkeypatch.setattr(runner_mod, "commit_all", commit_all)
    add(srv, "A", script({"write": {"path": "a-file.txt", "text": "from A"}}, {"sleep": 30}))
    add(srv, "B", deps=["A"])
    wait_for(lambda: task(store, "A").worktree and (Path(task(store, "A").worktree) / "a-file.txt").exists(),
             what="A записала файл")
    # act
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    committed = wait_events(store, "task_committed", "A")
    wait_events(store, "turn_finished", "B")
    # assert
    assert seen and seen[0] is not None
    finished = events(store, "turn_finished", "A")
    assert finished[0].data.get("stopped") is True and not events(store, "turn_failed", "A")
    assert task(store, "A").agent_state == "idle"
    assert run_git(git_repo, "log", "-1", "--format=%h", "A").strip() == committed[0].data["sha"]
    assert (Path(task(store, "B").worktree) / "a-file.txt").read_text() == "from A"


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
    wait_events(store, "turn_finished", "T-1")
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
    # arrange: агент проекта работает на любой стадии
    store = make_project(stage="decomposition")
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


def test_dependent_work_that_cannot_be_restored_stays_in_carry_ref(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "A", script({"write": {"path": "a.txt", "text": "A v1\n"}}))
    add(srv, "B", script({"write": {"path": "shared.txt", "text": "работа B\n"}}), deps=["A"])
    wait_events(store, "turn_finished", "A")
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "B")
    (Path(task(store, "A").worktree) / "shared.txt").write_text("версия A\n")
    stashes_before = run_git(git_repo, "stash", "list")
    # act
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "B", count=2)
    # assert
    ref = "refs/agent-board/B/carry"
    assert run_git(git_repo, "show", ref + "^3:shared.txt") == "работа B\n"
    assert run_git(git_repo, "stash", "list") == stashes_before == ""
    resume = [c for c in calls if c["kind"] == "resume" and c["session"] == task(store, "B").session_id]
    assert ref in resume[-1]["text"] and "Do not run any git commands" in resume[-1]["text"]
    assert any(ref in text for text in thread_texts(store, "B"))


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



# --- перезапуск ---

def restart(srv, start_server):
    srv.stop()
    return start_server()


def test_message_during_turn_survives_restart_and_old_process_is_killed(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"sleep": 20}))
    wait_events(store, "turn_started", "T-1")
    srv.board_for("pay").post_message("T-1", "user", "не потеряй меня")
    runner_sync(srv)
    old = calls[0]["proc"]
    # act
    srv = restart(srv, start_server)
    wait_events(store, "turn_finished", "T-1")
    # assert
    resume = [c for c in calls if c["kind"] == "resume"]
    assert len(resume) == 1
    assert "не потеряй меня" in resume[0]["text"] and "interrupted" in resume[0]["text"]
    assert resume[0]["alive_before"] == []
    assert old.wait(timeout=TIMEOUT) < 0
    assert resume[0]["text"].count("не потеряй меня") == 1


def test_graceful_stop_terminates_turn_and_next_start_resumes_it(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"sleep": 30}))
    wait_events(store, "turn_started", "T-1")
    runner_sync(srv)
    proc = calls[0]["proc"]
    # act
    srv.stop()
    # assert: процесс остановлен, карточка помнит идущий ход
    assert proc.poll() is not None
    t = task(store, "T-1")
    assert t.agent_state == "running" and t.turn_process["pgid"] == proc.pid
    assert not events(store, "turn_finished", "T-1")
    # act: следующий старт продолжает ход как прерванный
    start_server()
    wait_events(store, "turn_finished", "T-1")
    # assert
    wake = [e.data["reason"] for e in events(store, "agent_wake", "T-1")]
    assert wake.count("interrupted") == 1
    assert [c["kind"] for c in calls] == ["start", "resume"] and "interrupted" in calls[1]["text"]
    assert runner_mod.LEFTOVER_MISSING_TEXT not in thread_texts(store, "T-1")


def test_queued_task_pending_message_survives_restart(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 20}))
    wait_events(store, "turn_started", "A")
    add(srv, "B")
    wait_for(lambda: task(store, "B").agent_state == "waiting_turn", what="B в очереди")
    srv.board_for("pay").post_message("B", "user", "для B дословно")
    runner_sync(srv)
    # act
    srv = restart(srv, start_server)
    wait_events(store, "turn_finished", "B")
    # assert
    b_start = [c for c in calls if c["kind"] == "start" and c["env"]["AGENT_BOARD_TASK"] == "B"]
    assert len(b_start) == 1 and "для B дословно" in b_start[0]["text"]
    assert b_start[0]["alive_before"] == []


# --- одобрение с маркерами конфликта ---

def test_failed_approval_commit_returns_task_to_review_and_keeps_dependent_blocked(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "A", script({"write": {"path": "x.txt", "text": "<<<<<<< ours\nmine\n"}}, {"cli": ["status", "A", "in_review"]}))
    add(srv, "B", deps=["A"])
    wait_events(store, "turn_finished", "A")
    # act
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    wait_for(lambda: task(store, "A").status == Status.IN_REVIEW, what="A вернулась на ревью")
    runner_sync(srv)
    # assert
    assert not events(store, "task_committed", "A")
    assert any("x.txt" in t and "ревью" in t for t in thread_texts(store, "A"))
    assert not events(store, "turn_started", "B")


# --- ошибки подготовки ---

def test_prepare_error_keeps_reasons_until_config_fixed(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project(repos=[{"name": "one", "path": str(git_repo)}, {"name": "two", "path": str(git_repo)}])
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1")
    wait_for(lambda: task(store, "T-1").agent_state == "error", what="ошибка репозитория")
    board.post_message("T-1", "user", "важное уточнение")
    runner_sync(srv)
    assert task(store, "T-1").session_id is None
    assert "важное уточнение" in json.dumps(store.load_meta()["runner_pending"], ensure_ascii=False)
    # act
    board.update_config({"repos": [{"name": "one", "path": str(git_repo)}]})
    board.transition("T-1", Status.IN_PROGRESS, "user")
    wait_events(store, "turn_finished", "T-1")
    # assert
    assert [c["kind"] for c in calls] == ["start"]
    assert "важное уточнение" in calls[0]["text"]
    assert store.load_meta()["runner_pending"] == {}


def test_failure_after_turn_registered_still_completes_turn(make_project, start_server, calls, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    orig = type(board).record
    state = {"failed": False}

    def record(self, type, data):
        if type == "turn_started" and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("сбой записи")
        return orig(self, type, data)

    monkeypatch.setattr(type(board), "record", record)
    # act
    add(srv, "T-1")
    wait_events(store, "turn_finished", "T-1")
    board.post_message("T-1", "user", "ещё")
    wait_events(store, "turn_finished", "T-1", count=2)
    # assert
    assert [c["kind"] for c in calls] == ["start", "resume"]
    assert task(store, "T-1").agent_state == "idle"


def test_worker_exception_is_reported_on_the_card(make_project, start_server, calls, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1")
    wait_events(store, "turn_finished", "T-1")

    def boom(self, slug, key):
        raise RuntimeError("сломался коммит")

    monkeypatch.setattr(runner_mod.Runner, "_commit", boom)
    # act
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    # assert
    wait_for(lambda: any("сломался коммит" in t for t in thread_texts(store, "T-1")), what="сообщение об ошибке")


# --- лимиты и повторы ---

def test_project_agent_counts_in_global_limit(make_project, start_server, calls):
    # arrange
    store = make_project(global_max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 1}))
    wait_events(store, "turn_started", "A")
    # act
    srv.board_for("pay").post_message("_project", "user", "привет")
    wait_events(store, "turn_finished", "_project")
    # assert
    assert events(store, "turn_started", "_project")[0].seq > events(store, "turn_finished", "A")[0].seq


def test_project_agent_not_counted_in_project_limit(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    srv.board_for("pay").post_message("_project", "user", script({"sleep": 2}))
    wait_events(store, "turn_started", "_project")
    # act
    add(srv, "A")
    wait_events(store, "turn_finished", "A")
    wait_events(store, "turn_finished", "_project")
    # assert
    assert events(store, "turn_finished", "A")[0].seq < events(store, "turn_finished", "_project")[0].seq


def test_failed_turn_is_retried_after_delay(make_project, start_server, calls, monkeypatch):
    # arrange
    monkeypatch.setattr(runner_mod, "RETRY_DELAYS", (1.0, 1.0))
    store = make_project()
    srv = start_server()
    # act
    add(srv, "T-1", script({"fail": "boom"}))
    wait_events(store, "turn_failed", "T-1")
    failed_at = time.monotonic()
    runner_sync(srv)
    early = len(events(store, "turn_started", "T-1"))
    wait_events(store, "turn_started", "T-1", count=2)
    # assert
    assert early == 1
    assert time.monotonic() - failed_at >= 0.8


def test_recovery_does_not_signal_process_with_other_identity(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
        t = new_task_from_dict({"key": "T-1", "title": "Задача"})
        t.status, t.session_id, t.worktree, t.branch, t.base_branch = Status.IN_PROGRESS, "s-1", str(wt), "T-1", "main"
        t.agent_state, t.harness = "running", "fake"
        t.turn_process = {"pgid": stranger.pid, "started": "Mon Jan  1 00:00:00 2001", "mark": "s-1"}
        store.save_task(t)
        # act
        srv = start_server()
        wait_events(store, "turn_finished", "T-1")
        # assert
        assert stranger.poll() is None
        assert any("процесс хода не найден" in text for text in thread_texts(store, "T-1"))
    finally:
        stranger.kill()
        stranger.wait()


def test_recorded_identity_matches_running_turn_process(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    # act
    add(srv, "T-1", script({"sleep": 20}))
    wait_events(store, "turn_started", "T-1")
    runner_sync(srv)
    # assert
    proc = task(store, "T-1").turn_process
    assert proc["pgid"] == calls[0]["proc"].pid and proc["mark"] == calls[0]["session"]
    assert harness_mod.process_identity(proc["pgid"])["started"] == proc["started"]


def test_failure_after_start_turn_terminates_started_process(make_project, start_server, calls, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    orig = type(board).update_task

    def update_task(self, key, actor, fields):
        if set(fields) == {"session_id"}:
            raise RuntimeError("сбой записи сессии")
        return orig(self, key, actor, fields)

    monkeypatch.setattr(type(board), "update_task", update_task)
    # act
    add(srv, "T-1", script({"sleep": 20}))
    wait_for(lambda: any("сбой записи сессии" in t for t in thread_texts(store, "T-1")), what="ошибка запуска")
    runner_sync(srv)
    # assert
    assert len(calls) == 1
    assert calls[0]["proc"].wait(timeout=TIMEOUT) != 0
    assert not events(store, "turn_started", "T-1")


# --- поля раннера (P19) ---

def test_forged_worktree_is_not_committed_on_approval(make_project, start_server, calls, git_repo, tmp_path):
    # arrange
    store = make_project()
    forged = tmp_path / "forged"
    forged.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@e"]):
        run_git(forged, *args)
    (forged / "f.txt").write_text("x")
    run_git(forged, "add", "-A")
    run_git(forged, "commit", "-qm", "init")
    (forged / "f.txt").write_text("dirty")
    head = run_git(forged, "rev-parse", "HEAD")
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.status, t.worktree, t.branch, t.base_branch, t.harness = Status.IN_REVIEW, str(forged), "T-1", "main", "fake"
    store.save_task(t)
    srv = start_server()
    runner_sync(srv)
    # act
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    wait_for(lambda: task(store, "T-1").status == Status.IN_REVIEW, what="задача вернулась на ревью")
    runner_sync(srv)
    # assert
    assert run_git(forged, "rev-parse", "HEAD") == head
    assert run_git(forged, "status", "--porcelain").strip() == "M f.txt"
    assert not events(store, "task_committed", "T-1")
    assert any("worktree" in text for text in thread_texts(store, "T-1"))


def test_forged_worktree_is_not_rebased(make_project, start_server, calls, git_repo, tmp_path):
    # arrange
    store = make_project()
    a_wt = ensure_worktree(git_repo, "A", "A", "main", [])
    (a_wt / "a2.txt").write_text("A2\n")
    forged = ensure_worktree(git_repo, "OTHER", "OTHER", "main", [])
    (forged / "wip.txt").write_text("чужая работа\n")
    head = run_git(forged, "rev-parse", "HEAD")
    a = new_task_from_dict({"key": "A", "title": "A"})
    a.status, a.worktree, a.branch = Status.IN_REVIEW, str(a_wt), "A"
    store.save_task(a)
    b = new_task_from_dict({"key": "B", "title": "B", "deps": ["A"]})
    b.status, b.session_id, b.worktree, b.branch, b.base_branch, b.harness = (
        Status.IN_PROGRESS, "s-b", str(forged), "B", "main", "fake")
    store.save_task(b)
    srv = start_server()
    # act
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    wait_events(store, "task_committed", "A")
    wait_for(lambda: any("worktree" in text for text in thread_texts(store, "B")), what="ошибка на карточке B")
    runner_sync(srv)
    # assert
    assert run_git(forged, "rev-parse", "HEAD") == head
    assert (forged / "wip.txt").read_text() == "чужая работа\n"
    assert run_git(forged, "stash", "list").strip() == ""
    assert not [c for c in calls if c["cwd"] == forged]


# --- переписка агентов без пользователя (P18) ---

def test_agent_ping_pong_is_parked_after_five_wakes_until_user_acts(make_project, start_server, calls):
    # arrange
    store = make_project(project_session_id="p-1")
    srv = start_server()
    project_turns = lambda: len(events(store, "turn_started", "_project"))
    # act
    add(srv, "T-1", script({"pingpong": "T-1"}))
    wait_for(lambda: runner_mod.AGENTS_LOOP_TEXT in thread_texts(store, "_project"), what="остановка переписки")
    runner_sync(srv)
    wait_for(lambda: not srv.runner._turns, what="ходы закончились")
    runner_sync(srv)
    parked_turns = project_turns()
    # assert
    assert parked_turns == runner_mod.MAX_AGENT_WAKES
    assert len(events(store, "turn_started", "T-1")) == runner_mod.MAX_AGENT_WAKES + 1
    assert thread_texts(store, "_project").count(runner_mod.AGENTS_LOOP_TEXT) == 1
    # act: пользователь отвечает в треде — агент проекта снова работает
    srv.board_for("pay").post_message("_project", "user", "продолжайте")
    wait_for(lambda: runner_mod.AGENTS_LOOP_TEXT in thread_texts(store, "T-1"), what="остановка у задачи")
    wait_for(lambda: not srv.runner._turns, what="ходы закончились")
    # assert
    assert project_turns() == parked_turns + 1
    assert "продолжайте" in [c for c in calls if c["env"]["AGENT_BOARD_ACTOR"] == "project_agent"][-1]["text"]


# --- восстановление очереди и потерянные сессии ---

def test_waiting_task_with_session_resumes_with_queued_restart_text(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.status, t.session_id, t.worktree, t.branch, t.base_branch = Status.IN_PROGRESS, "s-1", str(wt), "T-1", "main"
    t.agent_state, t.harness = "waiting_turn", "fake"
    store.save_task(t)
    # act
    start_server()
    wait_events(store, "turn_finished", "T-1")
    # assert
    wake = [e.data["reason"] for e in events(store, "agent_wake", "T-1")]
    assert wake == ["queued_restart"]
    assert [(c["kind"], c["session"]) for c in calls] == [("resume", "s-1")]
    assert "interrupted" not in calls[0]["text"] and "waiting in the queue" in calls[0]["text"]


def test_lost_session_is_dropped_and_task_starts_fresh(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project()
    wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.status, t.session_id, t.worktree, t.branch, t.base_branch = Status.IN_PROGRESS, "s-gone", str(wt), "T-1", "main"
    t.harness = "fake"
    store.save_task(t)
    srv = start_server()
    runner_sync(srv)
    # act
    srv.board_for("pay").post_message("T-1", "user", script({"fail-if-resumed": "No conversation found with session ID: s-gone"}))
    wait_events(store, "turn_finished", "T-1", count=2)
    runner_sync(srv)
    # assert
    assert [(c["kind"], c["session"]) for c in calls][0] == ("resume", "s-gone")
    assert calls[1]["kind"] == "start" and calls[1]["session"] != "s-gone"
    assert "fail-if-resumed" in calls[1]["text"]
    t = task(store, "T-1")
    assert (t.session_id, t.agent_state, t.turn_error) == (calls[1]["session"], "idle", "")
    assert not events(store, "turn_failed", "T-1")
    assert srv.runner._failures.get(("pay", "T-1")) is None
    assert any("сесси" in text for text in thread_texts(store, "T-1"))


# --- одобренная задача и стадии проекта ---

def test_approved_task_gets_no_turn_until_returned(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1", script({"write": {"path": "f.txt", "text": "x"}}))
    wait_events(store, "turn_finished", "T-1")
    board.transition("T-1", Status.APPROVED, "user")
    wait_events(store, "task_committed", "T-1")
    # act
    board.post_message("T-1", "user", "поправь ещё заголовок")
    runner_sync(srv)
    # assert
    assert len(calls) == 1
    assert "поправь ещё заголовок" in thread_texts(store, "T-1")
    assert [r["reason"] for r in store.load_meta()["runner_pending"]["T-1"]] == ["message"]
    assert task(store, "T-1").agent_state == "idle"
    # act: пользователь возвращает задачу в работу
    board.transition("T-1", Status.IN_PROGRESS, "user")
    wait_events(store, "turn_finished", "T-1", count=2)
    runner_sync(srv)
    # assert
    assert [c["kind"] for c in calls] == ["start", "resume"]
    assert "поправь ещё заголовок" in calls[1]["text"]
    assert not store.load_meta()["runner_pending"]


def test_task_agents_start_only_in_work_stage(make_project, start_server, calls):
    # arrange: T-3 готова, но пробуждения о ней не было
    store = make_project(stage="decomposition")
    store.save_task(new_task_from_dict({"key": "T-3", "title": "Задача T-3"}))
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1")
    add(srv, "T-2")
    runner_sync(srv)
    board.set_stage("tracker", "user")
    runner_sync(srv)
    # assert: до стадии work агенты задач не стартуют, пробуждения копятся
    assert calls == []
    assert sorted(store.load_meta()["runner_pending"]) == ["T-1", "T-2"]
    assert {t.agent_state for t in store.load_tasks().values()} == {"idle"}
    # act
    board.set_stage("work", "user")
    for key in ("T-1", "T-2", "T-3"):
        wait_events(store, "turn_finished", key)
    # assert
    assert sorted(c["env"]["AGENT_BOARD_TASK"] for c in calls) == ["T-1", "T-2", "T-3"]
    assert {c["kind"] for c in calls} == {"start"}
