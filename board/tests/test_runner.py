from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from agent_board import harness as harness_mod
from agent_board import runner as runner_mod
from agent_board import ship as ship_module
from agent_board.board import new_task_from_dict
from agent_board.config import ProjectConfig
from agent_board.harness import FakeHarness
from agent_board.messages import CATALOG, msg
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
    assert env["AGENT_BOARD_LANG"] == "ru"
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
    waiting_reason = task(store, "B").wait_reason
    wait_events(store, "turn_finished", "B")
    # assert
    a_done = events(store, "turn_finished", "A")[0].seq
    b_started = events(store, "turn_started", "B")[0].seq
    assert b_started > a_done
    assert waiting_reason == "project"
    assert (task(store, "B").agent_state, task(store, "B").wait_reason) == ("idle", "")


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


@pytest.mark.parametrize("language, expected", [("en", "en"), ("ru", "ru")])
def test_running_task_on_start__project_language__interruption_note_in_that_language(
        make_project, start_server, calls, git_repo, monkeypatch, language, expected):
    # arrange: язык процесса противоположен языку проекта — сообщение должно следовать проекту
    monkeypatch.setenv("AGENT_BOARD_LANG", "ru" if language == "en" else "en")
    store = make_project(language=language)
    wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.status, t.session_id, t.worktree, t.branch, t.base_branch = Status.IN_PROGRESS, "s-1", str(wt), "T-1", "main"
    t.agent_state, t.harness = "running", "fake"
    store.save_task(t)
    # act
    start_server()
    wait_events(store, "turn_finished", "T-1")
    # assert
    assert msg("run.interrupted").render(expected) in thread_texts(store, "T-1")


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


def test_user_comment_on_task_during_decomposition_goes_to_project_agent(make_project, start_server, calls):
    # arrange
    store = make_project(stage="decomposition", project_session_id="p-1")
    srv = start_server()
    add(srv, "T-1")
    # act
    srv.board_for("pay").post_message("T-1", "user", "раздели на две")
    wait_events(store, "turn_finished", "_project")
    runner_sync(srv)
    # assert
    assert [(c["kind"], c["env"]["AGENT_BOARD_ACTOR"]) for c in calls] == [("resume", "project_agent")]
    assert "Comment from the user on task T-1" in calls[0]["text"] and "раздели на две" in calls[0]["text"]
    assert not events(store, "turn_started", "T-1")


def test_project_agent_state_changes_are_journaled(make_project, start_server, calls):
    # arrange
    store = make_project(stage="decomposition", project_session_id="p-1")
    srv = start_server()
    # act
    srv.board_for("pay").post_message("_project", "user", "привет")
    wait_events(store, "turn_finished", "_project")
    runner_sync(srv)
    # assert
    changes = [e.data for e in events(store, "project_agent_changed")]
    assert changes[-2:] == [{"state": "running", "parked": False, "wait_reason": ""},
                            {"state": "idle", "parked": False, "wait_reason": ""}]


def test_project_agent_changed_only_when_state_or_parking_changes(board):
    # arrange
    write = runner_mod.Runner._update_project_meta
    changes = lambda: [e.data for e in board.store.read_events() if e.type == "project_agent_changed"]

    # act
    write(board, {"project_agent_state": "idle"})
    write(board, {"project_agent_state": "running", "project_turn_error": ""})
    write(board, {"project_agent_state": "running", "project_turn_process": {"pid": 1}})
    write(board, {"project_agent_parked": True})
    write(board, {"project_agent_parked": True})

    # assert
    assert changes() == [{"state": "running", "parked": False, "wait_reason": ""},
                         {"state": "running", "parked": True, "wait_reason": ""}]


def test_project_agent_waiting_without_pending_is_reset_on_restart(make_project, start_server):
    # arrange
    store = make_project(stage="decomposition", project_session_id="p-1")
    meta = store.load_meta()
    meta.update({"project_agent_state": "waiting_turn", "project_wait_reason": "global"})
    store.save_meta(meta)
    # act
    srv = start_server()
    runner_sync(srv)
    # assert
    assert srv.board_for("pay").snapshot()["project_agent"]["state"] == "idle"
    assert srv.board_for("pay").snapshot()["project_agent"]["wait_reason"] == ""


def test_project_agent_parking_is_journaled(make_project, start_server):
    # arrange
    store = make_project(project_session_id="p-1")
    meta = store.load_meta()
    meta["project_agent_parked"] = True
    store.save_meta(meta)
    # act
    srv = start_server()
    runner_sync(srv)
    # assert
    assert {"state": "idle", "parked": False, "wait_reason": ""} in [e.data for e in events(store, "project_agent_changed")]


def test_work_stage_starts_project_agent_on_runner_start(make_project, start_server, calls):
    # arrange
    store = make_project(stage="work")
    # act
    start_server()
    wait_events(store, "turn_finished", "_project")
    # assert
    assert [c["kind"] for c in calls] == ["start"]
    assert store.load_config().project_session_id == calls[0]["session"]


def test_stage_work_wakes_project_agent_without_session(make_project, start_server, calls):
    # arrange
    store = make_project(stage="decomposition")
    srv = start_server()
    runner_sync(srv)
    assert calls == []
    # act
    srv.board_for("pay").set_stage("tracker", "user")
    srv.board_for("pay").set_stage("work", "user")
    wait_events(store, "turn_finished", "_project")
    # assert
    assert [c["kind"] for c in calls] == ["start"]
    assert store.load_config().project_session_id == calls[0]["session"]


def test_stage_work_does_not_wake_project_agent_with_session(make_project, start_server, calls):
    # arrange
    store = make_project(stage="decomposition", project_session_id="p-0")
    srv = start_server()
    # act
    srv.board_for("pay").set_stage("work", "user")
    runner_sync(srv)
    # assert
    assert calls == [] and not events(store, "turn_started", "_project")


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


def commit_to_main(git_repo, name, text):
    (git_repo / name).write_text(text)
    run_git(git_repo, "add", name)
    run_git(git_repo, "commit", "-m", name)


def test_rebase_request_waits_for_running_turn_and_carries_work_onto_fresh_base(make_project, start_server, calls,
                                                                               git_repo):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T", script({"sleep": 1.0}, {"write": {"path": "t.txt", "text": "работа T\n"}}))
    wait_events(store, "turn_started", "T")
    commit_to_main(git_repo, "fresh.txt", "свежая база\n")
    # act
    board.request_rebase("T", "user")
    wait_events(store, "turn_finished", "T", count=2)
    # assert
    wt = Path(task(store, "T").worktree)
    assert (wt / "fresh.txt").read_text() == "свежая база\n"
    assert (wt / "t.txt").read_text() == "работа T\n"
    assert run_git(wt, "rev-parse", "HEAD") == run_git(git_repo, "rev-parse", "main")
    assert [c["kind"] for c in calls] == ["start", "resume"] and calls[1]["alive_before"] == []
    assert "The user asked to bring your branch up to date" in calls[1]["text"]
    assert "moved your work onto main" in calls[1]["text"]


def test_rebase_request_conflict_keeps_work_in_carry_ref_with_card_message(make_project, start_server, calls,
                                                                           git_repo):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T", script({"write": {"path": "shared.txt", "text": "работа T\n"}}))
    wait_events(store, "turn_finished", "T")
    commit_to_main(git_repo, "shared.txt", "версия базы\n")
    # act
    board.request_rebase("T", "project_agent")
    wait_events(store, "turn_finished", "T", count=2)
    # assert
    ref = "refs/agent-board/T/carry"
    assert run_git(git_repo, "show", ref + "^3:shared.txt") == "работа T\n"
    assert run_git(git_repo, "stash", "list") == ""
    assert "The project agent asked" in calls[-1]["text"] and ref in calls[-1]["text"]
    assert any(ref in text for text in thread_texts(store, "T"))



def test_rebase_request_unfolds_approval_commit_of_returned_task_into_markers(make_project, start_server, calls,
                                                                              git_repo):
    # arrange: одобренная задача не влилась из-за конфликта с базой и вернулась в работу
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T", script({"write": {"path": "shared.txt", "text": "работа T\n"}}))
    wait_events(store, "turn_finished", "T")
    board.transition("T", Status.APPROVED, "user")
    wait_events(store, "task_committed", "T")
    commit_to_main(git_repo, "shared.txt", "версия базы\n")
    board.return_to_work("T", "user", "влив упал с конфликтом")
    wait_events(store, "turn_finished", "T", count=2)
    # act
    board.request_rebase("T", "user")
    wait_events(store, "turn_finished", "T", count=3)
    # assert
    wt = Path(task(store, "T").worktree)
    assert run_git(wt, "rev-parse", "HEAD") == run_git(git_repo, "rev-parse", "main")
    text = (wt / "shared.txt").read_text()
    assert "<<<<<<< " in text and "работа T" in text and "версия базы" in text
    assert run_git(git_repo, "for-each-ref", "refs/agent-board/") == ""
    assert "conflict markers: shared.txt" in calls[-1]["text"]
    assert "own commits on top of the old base" in calls[-1]["text"]
    assert any("разобраны в незакоммиченные" in t for t in thread_texts(store, "T"))


# --- влив в базовую ветку ---

def approve_with_work(store, srv, key, **extra):
    add(srv, key, script({"write": {"path": key + ".txt", "text": "работа " + key + "\n"}}), **extra)
    wait_events(store, "turn_finished", key)
    srv.board_for("pay").transition(key, Status.APPROVED, "user")
    wait_events(store, "task_committed", key)


def test_approved_task_merged_into_base_becomes_done_and_dependent_rebases(make_project, start_server, calls,
                                                                         git_repo, monkeypatch):
    # arrange
    monkeypatch.setattr(runner_mod, "MERGE_CHECK_SECONDS", 0.1)
    store = make_project(language="ru")
    srv = start_server()
    approve_with_work(store, srv, "A")
    add(srv, "B", deps=["A"])
    wait_events(store, "turn_finished", "B")
    # act
    run_git(git_repo, "merge", "--no-ff", "--no-edit", "A")
    wait_for(lambda: task(store, "A").status == Status.DONE, what="A в done")
    # assert
    changed = events(store, "status_changed", "A")[-1].data
    assert (changed["from"], changed["actor"]) == ("approved", "system")
    assert thread_texts(store, "A")[-1] == "Влито в main"
    wait_for(lambda: [e for e in events(store, "agent_wake", "B") if e.data.get("reason") == "rebase"],
             what="перенос B на базу")


def test_sent_task_merged_while_board_stopped_becomes_done_on_start(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project(language="en")
    srv = start_server()
    approve_with_work(store, srv, "A")
    srv.board_for("pay").transition("A", Status.SENT, "system")
    srv.stop()
    run_git(git_repo, "merge", "--no-ff", "--no-edit", "A")
    # act
    srv = start_server()
    runner_sync(srv)
    # assert
    assert task(store, "A").status == Status.DONE
    assert thread_texts(store, "A")[-1] == "Merged into main"


def test_approved_task_not_merged_stays_approved(make_project, start_server, calls, git_repo, monkeypatch):
    # arrange
    monkeypatch.setattr(runner_mod, "MERGE_CHECK_SECONDS", 0.05)
    store = make_project()
    srv = start_server()
    approve_with_work(store, srv, "A")
    commit_to_main(git_repo, "other.txt", "чужая работа\n")
    # act
    time.sleep(0.5)
    runner_sync(srv)
    # assert
    assert task(store, "A").status == Status.APPROVED
    assert not [m for m in store.read_thread("A") if m.author == "system" and "main" in m.text]


def test_approved_branch_without_commits_is_not_taken_as_merged(make_project, start_server, calls, git_repo,
                                                               monkeypatch):
    # arrange
    monkeypatch.setattr(runner_mod, "MERGE_CHECK_SECONDS", 0.05)
    store = make_project()
    srv = start_server()
    add(srv, "A")
    wait_events(store, "turn_finished", "A")
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    # act
    time.sleep(0.5)
    runner_sync(srv)
    # assert
    assert not events(store, "task_committed", "A")
    assert run_git(git_repo, "merge-base", "--is-ancestor", "A", "main") == ""
    assert task(store, "A").status == Status.APPROVED


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
    assert CATALOG["run.leftover_missing"][1] not in thread_texts(store, "T-1")


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


def test_commit_failure_rollback_delivers_reasons_accumulated_during_approval(make_project, start_server, calls, monkeypatch):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "A", script({"write": {"path": "a.txt", "text": "A\n"}}, {"cli": ["status", "A", "in_review"]}))
    wait_events(store, "turn_finished", "A")
    runner_sync(srv)

    def failing_commit(path, message):
        # пока задача одобрена, пользователь успевает написать в тред
        board.post_message("A", "user", "поправь заголовок")
        raise runner_mod.GitError("не закоммитилось")

    monkeypatch.setattr(runner_mod, "commit_all", failing_commit)
    # act
    board.transition("A", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "A", count=2)
    # assert
    assert task(store, "A").status == Status.IN_REVIEW
    assert [c["kind"] for c in calls] == ["start", "resume"]
    assert "поправь заголовок" in calls[1]["text"]


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

def test_project_agent_runs_over_full_global_limit(make_project, start_server, calls):
    # arrange
    store = make_project(global_max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 2}))
    wait_events(store, "turn_started", "A")
    # act
    srv.board_for("pay").post_message("_project", "user", "привет")
    wait_events(store, "turn_finished", "_project")
    runner_sync(srv)
    # assert
    assert not events(store, "turn_finished", "A")


def test_task_waits_for_global_limit_while_project_agent_runs(make_project, start_server, calls):
    # arrange
    store = make_project(global_max_agents=1)
    srv = start_server()
    srv.board_for("pay").post_message("_project", "user", script({"sleep": 2}))
    add(srv, "A", script({"sleep": 2}))
    wait_events(store, "turn_started", "A")
    # act
    add(srv, "B")
    wait_events(store, "turn_finished", "B")
    # assert
    assert events(store, "turn_started", "B")[0].seq > events(store, "turn_finished", "A")[0].seq


def test_raised_limit_starts_queued_task_without_waiting_for_running_turn(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 5}))
    wait_events(store, "turn_started", "A")
    add(srv, "B")
    wait_for(lambda: task(store, "B").wait_reason == "project", what="B в очереди")
    # act
    srv.board_for("pay").set_config("user", {"max_agents": 2})
    wait_events(store, "turn_finished", "B")
    # assert
    assert not events(store, "turn_finished", "A")



def test_queued_task_wait_reason_follows_the_limit_that_blocks_it(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1, global_max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 4}))
    wait_events(store, "turn_started", "A")
    add(srv, "B")
    wait_for(lambda: task(store, "B").agent_state == "waiting_turn", what="B в очереди")
    first = task(store, "B").wait_reason
    # act
    srv.board_for("pay").set_config("user", {"global_max_agents": 2})
    wait_for(lambda: task(store, "B").wait_reason == "project", what="B упёрся в лимит проекта")
    wait_events(store, "turn_finished", "B")
    # assert
    assert first == "global"
    assert events(store, "turn_started", "B")[0].seq > events(store, "turn_finished", "A")[0].seq
    assert task(store, "B").wait_reason == ""


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
    assert datetime.fromisoformat(proc["at"]).tzinfo is not None, "время начала хода для интерфейса"


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
    wait_for(lambda: CATALOG["run.agents_loop"][1] in thread_texts(store, "_project"), what="остановка переписки")
    runner_sync(srv)
    wait_for(lambda: not srv.runner._turns, what="ходы закончились")
    runner_sync(srv)
    parked_turns = project_turns()
    # assert
    assert parked_turns == runner_mod.MAX_AGENT_WAKES
    assert len(events(store, "turn_started", "T-1")) == runner_mod.MAX_AGENT_WAKES + 1
    assert thread_texts(store, "_project").count(CATALOG["run.agents_loop"][1]) == 1
    assert srv.board_for("pay").snapshot()["project_agent"]["parked"] is True
    # act: пользователь отвечает в треде — агент проекта снова работает
    srv.board_for("pay").post_message("_project", "user", "продолжайте")
    wait_for(lambda: CATALOG["run.agents_loop"][1] in thread_texts(store, "T-1"), what="остановка у задачи")
    wait_for(lambda: not srv.runner._turns, what="ходы закончились")
    runner_sync(srv)
    # assert
    assert project_turns() == parked_turns + 1
    snap = srv.board_for("pay").snapshot()
    assert snap["project_agent"]["parked"] is False
    assert [t["agent_parked"] for t in snap["tasks"]] == [True]
    assert "продолжайте" in [c for c in calls if c["env"]["AGENT_BOARD_ACTOR"] == "project_agent"][-1]["text"]


def _park_task_agent(store, srv):
    """Доводит переписку агентов до остановки у задачи T-1."""
    add(srv, "T-1", script({"pingpong": "T-1"}))
    wait_for(lambda: CATALOG["run.agents_loop"][1] in thread_texts(store, "_project"), what="остановка переписки")
    srv.board_for("pay").post_message("_project", "user", "продолжайте")
    wait_for(lambda: CATALOG["run.agents_loop"][1] in thread_texts(store, "T-1"), what="остановка у задачи")
    wait_for(lambda: not srv.runner._turns, what="ходы закончились")
    runner_sync(srv)
    assert store.load_tasks()["T-1"].agent_parked is True


def test_user_message_clears_task_agent_parked(make_project, start_server, calls):
    # arrange
    store = make_project(project_session_id="p-1")
    srv = start_server()
    _park_task_agent(store, srv)
    # act
    srv.board_for("pay").post_message("T-1", "user", "смотри сюда")
    runner_sync(srv)
    # assert
    wait_for(lambda: store.load_tasks()["T-1"].agent_parked is False, what="флаг остановки снят")


def test_user_status_change_clears_task_agent_parked(make_project, start_server, calls):
    # arrange
    store = make_project(project_session_id="p-1")
    srv = start_server()
    _park_task_agent(store, srv)
    # act
    srv.board_for("pay").transition("T-1", Status.CANCELLED, "user")
    runner_sync(srv)
    # assert
    wait_for(lambda: store.load_tasks()["T-1"].agent_parked is False, what="флаг остановки снят")


def test_restart_clears_parked_flags(make_project, start_server):
    # arrange: счётчик переписки живёт в памяти, после перезапуска остановка снята
    store = make_project(project_session_id="p-1")
    t = new_task_from_dict({"key": "T-1", "title": "Задача"})
    t.agent_parked = True
    store.save_task(t)
    meta = store.load_meta()
    meta["project_agent_parked"] = True
    store.save_meta(meta)
    # act
    srv = start_server()
    runner_sync(srv)
    # assert
    snap = srv.board_for("pay").snapshot()
    assert snap["project_agent"]["parked"] is False
    assert snap["tasks"][0]["agent_parked"] is False


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
    store = make_project(stage="decomposition", project_session_id="p-0")
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


# --- ревью ---

from fakes import fake_crit  # noqa: E402


@pytest.fixture
def crit_bin(tmp_path):
    yield fake_crit.install(tmp_path / "crit-bin")
    fake_crit.cleanup(tmp_path / "crit-bin")


def review_project(make_project, crit_bin, **kw):
    kw.setdefault("review", {"tool": "crit", "binary": str(crit_bin)})
    return make_project(**kw)


def to_review(srv, store, key="A"):
    add(srv, key, script({"write": {"path": "f.txt", "text": "x"}}, {"cli": ["status", key, "in_review"]}))
    wait_for(lambda: task(store, key).status == Status.IN_REVIEW and task(store, key).agent_state == "idle",
             what="%s на ревью" % key)
    runner_sync(srv)


def review(store, key="A"):
    return task(store, key).review or {}


def wait_round(store, rnd, key="A"):
    """Раунд открыт, ссылка известна, и crit этого раунда уже запущен."""
    def opened():
        r = review(store, key)
        started = {c["pid"] for c in fake_crit.review_calls(store.root / "crit")}
        return r.get("state") == "open" and r.get("round") == rnd and r.get("url") \
            and (r.get("process") or {}).get("pgid") in started and r
    return wait_for(opened, what="раунд %d открыт" % rnd)


def crit_dir(store):
    return store.root / "crit"


def test_review_round_with_comments_wakes_agent_then_opens_next_round(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    # act
    fake_crit.finish(crit_dir(store), first["session"], [{"path": "f.txt", "line": 1, "body": "переименуй файл"}])
    second = wait_round(store, 2)
    runner_sync(srv)
    # assert
    wakes = [e.data for e in events(store, "agent_wake", "A") if e.data["reason"] == "review"]
    assert len(wakes) == 1 and wakes[0]["round"] == 1
    assert [(c["path"], c["line"], c["body"]) for c in wakes[0]["comments"]] == [("f.txt", 1, "переименуй файл")]
    resume = [c for c in calls if c["kind"] == "resume"]
    assert len(resume) == 1
    assert "переименуй файл" in resume[0]["text"] and "--reply-to" in resume[0]["text"]
    assert str(crit_dir(store)) in resume[0]["text"]
    assert second["url"] == first["url"] and second["session"] == first["session"]
    assert [(h["round"], h["state"], h["comments"]) for h in second["history"]] == [(1, "changes", 1)]
    rounds = fake_crit.review_calls(crit_dir(store))
    assert len(rounds) == 2
    assert rounds[0]["cwd"] == task(store, "A").worktree and "--session" not in rounds[0]["argv"]
    assert rounds[1]["argv"][-2:] == ["--session", first["session"]] and "-p" in rounds[1]["argv"]
    assert "--base-branch" in rounds[0]["argv"] and "main" in rounds[0]["argv"]
    assert task(store, "A").status == Status.IN_REVIEW


def test_review_round_without_comments_approves_as_user_and_commits(make_project, start_server, calls, crit_bin,
                                                                    git_repo):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    # act
    fake_crit.finish(crit_dir(store), first["session"])
    committed = wait_events(store, "task_committed", "A")
    runner_sync(srv)
    # assert
    a = task(store, "A")
    assert a.status == Status.APPROVED
    changed = [e.data for e in events(store, "status_changed", "A") if e.data["to"] == "approved"]
    assert changed == [{"key": "A", "from": "in_review", "to": "approved", "actor": "user"}]
    assert "Одобрено в ревью (раунд 1)" in thread_texts(store, "A")
    assert a.review["state"] == "approved" and a.review["process"] is None
    assert [(h["round"], h["state"], h["comments"]) for h in a.review["history"]] == [(1, "approved", 0)]
    assert run_git(git_repo, "log", "-1", "--format=%h", "A").strip() == committed[0].data["sha"]
    assert [c["kind"] for c in calls] == ["start"]


def test_restart_during_round_reconnects_with_same_url(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    old_pid = fake_crit.review_calls(crit_dir(store))[0]["pid"]
    # act
    srv = restart(srv, start_server)
    wait_for(lambda: len(fake_crit.review_calls(crit_dir(store))) == 2, what="переподключение к раунду")
    again = wait_round(store, 1)
    # assert
    assert again["url"] == first["url"] and again["session"] == first["session"]
    assert fake_crit.review_calls(crit_dir(store))[1]["argv"][-2:] == ["--session", first["session"]]
    assert not harness_mod.group_alive(old_pid)
    assert again["process"]["pgid"] != old_pid
    # act: раунд, открытый до перезапуска, завершается как обычно
    fake_crit.finish(crit_dir(store), first["session"])
    wait_events(store, "task_committed", "A")
    assert task(store, "A").status == Status.APPROVED


def test_restart_after_review_page_is_gone_reopens_round_on_same_url(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    srv.stop()
    fake_crit.stop_daemon(crit_dir(store), first["session"])
    # act
    start_server()
    wait_for(lambda: len(fake_crit.review_calls(crit_dir(store))) == 3, what="повторный запуск раунда")
    again = wait_round(store, 1)
    # демон нового запуска регистрируется чуть позже записи раунда
    wait_for(lambda: fake_crit.daemon(crit_dir(store), first["session"]) is not None, what="демон ревью после перезапуска")
    # assert
    argv = [c["argv"] for c in fake_crit.review_calls(crit_dir(store))]
    assert "--session" in argv[1] and "--session" not in argv[2]
    assert argv[2][argv[2].index("-p") + 1] == str(fake_crit.daemon(crit_dir(store), first["session"])["port"])
    assert again["url"] == first["url"] and again["round"] == 1


def test_leftover_review_client_from_crashed_server_is_killed_before_reconnect(make_project, start_server, calls,
                                                                              crit_bin, monkeypatch):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    old_pid = fake_crit.review_calls(crit_dir(store))[0]["pid"]
    # сервер «упал»: клиент раунда пережил его
    kill_reviews = runner_mod.Runner._kill_reviews
    runner_mod.Runner._kill_reviews = lambda self: None
    try:
        srv.stop()
    finally:
        runner_mod.Runner._kill_reviews = kill_reviews
    assert harness_mod.group_alive(old_pid)
    # act
    start_server()
    wait_for(lambda: len(fake_crit.review_calls(crit_dir(store))) == 2, what="переподключение")
    wait_round(store, 1)
    # assert
    wait_for(lambda: not harness_mod.group_alive(old_pid), what="старый клиент остановлен")


def test_repeated_review_request_starts_one_process(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    # act: второй клик приходит раньше, чем раннер успел открыть раунд
    board.request_review("A", "user")
    board.request_review("A", "user")
    wait_round(store, 1)
    runner_sync(srv)
    with pytest.raises(ValueError):
        board.request_review("A", "user")
    runner_sync(srv)
    # assert
    assert len(fake_crit.review_calls(crit_dir(store))) == 1
    assert review(store)["round"] == 1


def test_missing_review_binary_shows_error_on_card(make_project, start_server, calls, tmp_path):
    # arrange
    store = make_project(review={"tool": "crit", "binary": str(tmp_path / "no-crit")})
    srv = start_server()
    to_review(srv, store)
    # act
    srv.board_for("pay").request_review("A", "user")
    wait_for(lambda: review(store).get("error"), what="ошибка ревью на карточке")
    runner_sync(srv)
    # assert
    r = review(store)
    assert r["state"] == "idle" and "не найден" in r["error"]
    assert any("Ревью не запущено" in t and "не найден" in t for t in thread_texts(store, "A"))
    assert task(store, "A").status == Status.IN_REVIEW
    # раннер продолжает работать
    srv.board_for("pay").post_message("A", "user", "ещё живой?")
    wait_events(store, "turn_finished", "A", count=2)


def test_manual_approve_during_round_stops_crit_and_commits(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    pid = fake_crit.review_calls(crit_dir(store))[0]["pid"]
    # act
    srv.board_for("pay").approve("A", "user")
    wait_events(store, "task_committed", "A")
    runner_sync(srv)
    # assert
    wait_for(lambda: not harness_mod.group_alive(pid), what="crit остановлен")
    r = review(store)
    assert r["state"] == "approved" and r["process"] is None


def test_return_during_round_stops_crit_and_resets_review(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    pid = fake_crit.review_calls(crit_dir(store))[0]["pid"]
    # act
    srv.board_for("pay").return_to_work("A", "user", "доделай")
    wait_events(store, "turn_finished", "A", count=2)
    runner_sync(srv)
    # assert
    wait_for(lambda: not harness_mod.group_alive(pid), what="crit остановлен")
    assert review(store)["state"] == "idle"
    assert len(fake_crit.review_calls(crit_dir(store))) == 1


def test_no_review_tool_approve_and_return_with_comment(make_project, start_server, calls, git_repo):
    # arrange
    store = make_project(review={"tool": "none"})
    srv = start_server()
    to_review(srv, store, "A")
    to_review(srv, store, "B")
    board = srv.board_for("pay")
    # act
    board.approve("A", "user")
    board.return_to_work("B", "user", "добавь тест на пустой ввод")
    committed = wait_events(store, "task_committed", "A")
    wait_events(store, "turn_finished", "B", count=2)
    runner_sync(srv)
    # assert
    assert task(store, "A").status == Status.APPROVED
    assert run_git(git_repo, "log", "-1", "--format=%h", "A").strip() == committed[0].data["sha"]
    resumes = [c for c in calls if c["kind"] == "resume" and c["env"]["AGENT_BOARD_TASK"] == "B"]
    assert len(resumes) == 1
    assert "добавь тест на пустой ввод" in resumes[0]["text"] and "in_review" in resumes[0]["text"]
    assert task(store, "B").status == Status.IN_PROGRESS


def test_next_round_waits_while_agent_asks_and_opens_after_it_returns_to_review(make_project, start_server, calls,
                                                                                crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    board.request_review("A", "user")
    first = wait_round(store, 1)
    ask = script({"cli": ["ask", "A", "--text", "Как назвать?", "--option", "x", "--option", "y"]})
    # act: по комментарию агент задаёт вопрос и заканчивает ход
    fake_crit.finish(crit_dir(store), first["session"], [{"path": "f.txt", "line": 1, "body": ask}])
    wait_for(lambda: task(store, "A").status == Status.WAITING_ANSWER and task(store, "A").agent_state == "idle",
             what="агент ждёт ответа")
    runner_sync(srv)
    # assert
    assert review(store)["state"] == "fixing"
    assert len(fake_crit.review_calls(crit_dir(store))) == 1
    # act: после ответа агент возвращает задачу на ревью — раннер открывает следующий раунд
    qid = task(store, "A").questions[0].id
    board.answer("A", qid, "x", script({"cli": ["status", "A", "in_review"]}))
    second = wait_round(store, 2)
    # assert
    assert second["url"] == first["url"]
    assert len(fake_crit.review_calls(crit_dir(store))) == 2


def test_approval_drops_undelivered_review_comments(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    board.request_review("A", "user")
    first = wait_round(store, 1)
    board.post_message("A", "user", script({"sleep": 3}))
    wait_for(lambda: task(store, "A").agent_state == "running", what="агент занят")
    fake_crit.finish(crit_dir(store), first["session"], [{"path": "f.txt", "line": 1, "body": "устаревшее замечание"}])
    wait_for(lambda: review(store).get("state") == "changes", what="раунд с замечаниями")
    # act
    board.approve("A", "user")
    wait_events(store, "task_committed", "A")
    board.return_to_work("A", "user", "ещё одна правка")
    wait_events(store, "turn_finished", "A", count=3)
    runner_sync(srv)
    # assert
    resumes = [c["text"] for c in calls if c["kind"] == "resume"]
    assert "ещё одна правка" in resumes[-1]
    assert not any("устаревшее замечание" in t for t in resumes)



def busy_agent(srv, store, key="A", seconds=3):
    srv.board_for("pay").post_message(key, "user", script({"sleep": seconds}))
    wait_for(lambda: task(store, key).agent_state == "running", what="агент занят")


@pytest.mark.parametrize("action", ["approve", "cancel", "return"])
@pytest.mark.parametrize("phase", ["changes", "next_round"])
def test_closing_review_stops_crit_daemon(make_project, start_server, calls, crit_bin, action, phase):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    board.request_review("A", "user")
    first = wait_round(store, 1)
    if phase == "changes":
        busy_agent(srv, store)
    fake_crit.finish(crit_dir(store), first["session"], [{"path": "f.txt", "line": 1, "body": "замечание"}])
    if phase == "changes":
        wait_for(lambda: review(store).get("state") == "changes", what="раунд с замечаниями")
    else:
        wait_round(store, 2)
    daemon = fake_crit.daemon_pid(crit_dir(store), first["session"])
    assert daemon
    # act
    if action == "approve":
        board.approve("A", "user")
    elif action == "cancel":
        board.transition("A", Status.CANCELLED, "user")
    else:
        board.return_to_work("A", "user", "доделай")
    # assert
    wait_for(lambda: not harness_mod.group_alive(daemon), what="демон crit остановлен")
    assert fake_crit.daemon(crit_dir(store), first["session"]) is None


def test_review_approved_during_agent_turn_is_left_to_the_user(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    busy_agent(srv, store)
    # act
    fake_crit.finish(crit_dir(store), first["session"])
    wait_for(lambda: CATALOG["run.review_during_turn"][1] in thread_texts(store, "A"), what="сообщение на карточке")
    wait_events(store, "turn_finished", "A", count=2)
    runner_sync(srv)
    # assert
    a = task(store, "A")
    assert a.status == Status.IN_REVIEW and not events(store, "task_committed", "A")
    assert a.review["state"] == "idle" and a.review["history"][-1]["state"] == "approved"
    assert len(fake_crit.review_calls(crit_dir(store))) == 1


def test_review_approved_when_task_left_review_reports_on_card(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    board.request_review("A", "user")
    first = wait_round(store, 1)
    board.transition("A", Status.IN_PROGRESS, "agent")
    runner_sync(srv)
    # act
    fake_crit.finish(crit_dir(store), first["session"])
    wait_for(lambda: review(store).get("state") == "idle", what="раунд закрыт")
    runner_sync(srv)
    # assert
    r = review(store)
    assert r["error"] and task(store, "A").status == Status.IN_PROGRESS
    assert any("не на ревью" in t for t in thread_texts(store, "A"))


def test_next_round_not_opened_for_agent_in_error(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    board = srv.board_for("pay")
    board.update_task("A", "system", {"agent_state": "error", "review": {"round": 1, "state": "fixing"}})
    runner_sync(srv)
    # act
    srv.runner._maybe_next_round(("pay", "A"))
    runner_sync(srv)
    # assert
    assert fake_crit.review_calls(crit_dir(store)) == [] and review(store)["state"] == "fixing"


def test_recovery_closes_open_round_when_review_tool_removed(make_project, start_server, calls, crit_bin):
    # arrange
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    srv.stop()
    srv.board_for("pay").update_config({"review": {"tool": "none"}})
    # act
    srv = start_server()
    wait_for(lambda: review(store).get("state") == "idle", what="раунд закрыт")
    runner_sync(srv)
    # assert
    assert review(store)["error"] and len(fake_crit.review_calls(crit_dir(store))) == 1
    assert any("ревью" in t and "отключ" in t for t in thread_texts(store, "A"))


def test_review_base_falls_back_to_repo_base(make_project, start_server, calls, crit_bin, git_repo):
    # arrange
    run_git(git_repo, "branch", "trunk")
    store = review_project(make_project, crit_bin, repos=[{"name": "app", "path": str(git_repo), "base": "trunk"}])
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").update_task("A", "system", {"base_branch": None})
    # act
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    # assert
    argv = fake_crit.review_calls(crit_dir(store))[0]["argv"]
    assert argv[argv.index("--base-branch") + 1] == "trunk"


# --- отправка в ревью ---

def approved(srv, store, key, **extra):
    add(srv, key, script({"write": {"path": key + ".txt", "text": key}}, {"cli": ["status", key, "in_review"]}), **extra)
    wait_events(store, "turn_finished", key)
    srv.board_for("pay").transition(key, Status.APPROVED, "user")
    wait_events(store, "task_committed", key)


def wait_ship(store, key, states=("done", "error", "skipped", "interrupted")):
    return wait_for(lambda: (task(store, key).ship or {}).get("state") in states and task(store, key).ship,
                    what="ship %s %s" % (key, states))


def send(srv, *keys):
    return srv.board_for("pay").request_ship(list(keys), "user")


def test_ship_command_quotes_title_and_runs_in_worktree(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(ship={"command": "printf '%s|%s|%s\\n' {title} \"$AGENT_BOARD_TASK\" \"$PWD\" > " + str(out)})
    srv = start_server()
    add(srv, "T-1", script({"write": {"path": "f.txt", "text": "x"}}, {"cli": ["status", "T-1", "in_review"]}), title="it's \"q\" $(touch pwned) x")
    wait_events(store, "turn_finished", "T-1")
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    wait_events(store, "task_committed", "T-1")
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    # assert
    title, key, cwd = out.read_text().rstrip("\n").split("|")
    assert state["state"] == "done" and state["exit_code"] == 0 and state["at"]
    assert title == "it's \"q\" $(touch pwned) x" and key == "T-1"
    assert Path(cwd).resolve() == Path(task(store, "T-1").worktree).resolve()
    assert not (Path(task(store, "T-1").worktree) / "pwned").exists()
    assert task(store, "T-1").status == Status.SENT


def test_ship_chain_runs_in_dependency_order_with_dependency_as_base(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "order.txt"
    store = make_project(ship={"command": "echo {key} {base} | tee -a " + str(out)})
    srv = start_server()
    approved(srv, store, "A")
    approved(srv, store, "B", deps=["A"])
    # act
    send(srv, "B", "A")
    wait_ship(store, "B")
    # assert
    assert out.read_text().splitlines() == ["A main", "B A"]
    assert task(store, "A").status == task(store, "B").status == Status.SENT
    assert [e.data["key"] for e in events(store, "ship_started")] == ["A", "B"]
    finished = events(store, "ship_finished")
    assert [e.data for e in finished] == [{"key": "A", "ok": True, "exit_code": 0},
                                          {"key": "B", "ok": True, "exit_code": 0}]
    assert "A main" in (store.root / "logs" / "A.ship.log").read_text()
    assert "A main" in task(store, "A").ship["output_tail"]


def test_ship_failure_skips_dependents_and_keeps_independent_then_retry_succeeds(make_project, start_server, calls,
                                                                              tmp_path):
    # arrange
    flag = tmp_path / "fixed"
    store = make_project(ship={"command": "echo out-{key}; if [ {key} = A ] && [ ! -e %s ]; then echo boom >&2; exit 3; fi" % flag})
    srv = start_server()
    approved(srv, store, "A")
    approved(srv, store, "B", deps=["A"])
    approved(srv, store, "C")
    # act
    send(srv, "A", "B", "C")
    wait_ship(store, "B")
    wait_ship(store, "C")
    # assert
    a = task(store, "A")
    assert a.status == Status.APPROVED and a.ship["state"] == "error" and a.ship["exit_code"] == 3
    assert "boom" in a.ship["output_tail"]
    assert task(store, "B").ship["state"] == "skipped" and "A" in task(store, "B").ship["reason"]
    assert task(store, "B").status == Status.APPROVED
    assert task(store, "C").status == Status.SENT
    assert any("код выхода 3" in t and "boom" in t for t in thread_texts(store, "A"))
    assert [e.data for e in events(store, "ship_finished", "A")] == [{"key": "A", "ok": False, "exit_code": 3}]
    # act: повтор после исправления
    flag.write_text("")
    send(srv, "A", "B")
    wait_for(lambda: task(store, "B").status == Status.SENT, what="B отправлена")
    # assert
    assert task(store, "A").status == Status.SENT and task(store, "A").ship["state"] == "done"


def test_ship_timeout_kills_process_group(make_project, start_server, calls, tmp_path):
    # arrange
    pidfile = tmp_path / "child.pid"
    store = make_project(ship={"command": "sleep 300 & echo $! > %s; echo started; wait" % pidfile, "timeout": 1})
    srv = start_server()
    approved(srv, store, "T-1")
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    # assert
    pid = int(pidfile.read_text())
    wait_for(lambda: not _alive(pid), what="процесс потомка завершён")
    assert state["state"] == "error" and state["exit_code"] is None and "started" in state["output_tail"]
    assert task(store, "T-1").status == Status.APPROVED
    assert any("таймаут" in t for t in thread_texts(store, "T-1"))


def test_ship_failure__on_failure_return__conflict_returns_task_to_agent_then_reapproval_ships(
        make_project, start_server, calls, git_repo, tmp_path):
    # arrange: команда падает один раз, а в выводе — сценарий следующего хода агента
    flag = tmp_path / "shipped-once"
    resume = script({"write": {"path": "shared.txt", "text": "работа T поверх базы\n"}},
                    {"cli": ["status", "T", "in_review"]})
    command = tmp_path / "ship.sh"
    command.write_text("if [ -e %s ]; then echo sent; exit 0; fi\ntouch %s\necho 'CONFLICT shared.txt'\necho '%s'\n"
                       "exit 1\n" % (flag, flag, resume))
    store = make_project(ship={"command": "bash " + str(command), "on_approve": True, "on_failure": "return"})
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T", script({"write": {"path": "shared.txt", "text": "работа T\n"}}, {"cli": ["status", "T", "in_review"]}))
    wait_events(store, "turn_finished", "T")
    commit_to_main(git_repo, "shared.txt", "версия базы\n")
    main_tip = run_git(git_repo, "rev-parse", "main")
    # act
    board.transition("T", Status.APPROVED, "user")
    wait_events(store, "turn_finished", "T", count=2)
    # assert: задача у агента, ветка на свежей базе, конфликт — маркерами в файлах
    wake = [e.data for e in events(store, "agent_wake", "T") if e.data.get("reason") == "ship_failed"]
    assert len(wake) == 1 and wake[0]["exit_code"] == 1 and "CONFLICT shared.txt" in wake[0]["output"]
    assert [e.data for e in events(store, "status_changed", "T")][-2]["actor"] == "system"
    text = calls[-1]["text"]
    assert calls[-1]["kind"] == "resume" and "exit code 1" in text and "CONFLICT shared.txt" in text
    assert "conflict markers: shared.txt" in text and "status T in_review" in text
    assert task(store, "T").status == Status.IN_REVIEW
    wt = Path(task(store, "T").worktree)
    assert run_git(wt, "rev-parse", "HEAD") == main_tip
    assert any("возвращена в работу" in t for t in thread_texts(store, "T"))
    assert any("разобраны в незакоммиченные" in t for t in thread_texts(store, "T"))
    # act: повторное одобрение отправляет задачу как обычно
    board.transition("T", Status.APPROVED, "user")
    wait_for(lambda: task(store, "T").status == Status.SENT, what="T отправлена")
    # assert
    assert run_git(wt, "rev-parse", "HEAD^") == main_tip
    assert run_git(wt, "show", "HEAD:shared.txt") == "работа T поверх базы\n"


def test_ship_failure__on_failure_stay__task_stays_approved_without_wake(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "exit 2", "on_failure": "stay"})
    srv = start_server()
    approved(srv, store, "T-1")
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    runner_sync(srv)
    # assert
    assert state["state"] == "error" and state["exit_code"] == 2
    assert task(store, "T-1").status == Status.APPROVED
    assert not [e for e in events(store, "agent_wake", "T-1") if e.data.get("reason") == "ship_failed"]


def test_ship_timeout__on_failure_return__task_stays_approved(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "sleep 300", "timeout": 1, "on_failure": "return"})
    srv = start_server()
    approved(srv, store, "T-1")
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    runner_sync(srv)
    # assert
    assert state["state"] == "error" and state["exit_code"] is None
    assert task(store, "T-1").status == Status.APPROVED
    assert not [e for e in events(store, "agent_wake", "T-1") if e.data.get("reason") == "ship_failed"]


def test_cancel_during_ship__on_failure_return__task_is_not_returned(make_project, start_server, calls, tmp_path):
    # arrange
    started = tmp_path / "started"
    store = make_project(ship={"command": "touch %s; sleep 300" % started, "on_failure": "return"})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_for(started.exists, what="команда стартовала")
    # act
    srv.board_for("pay").transition("T-1", Status.CANCELLED, "user")
    wait_ship(store, "T-1", states=("cancelled",))
    runner_sync(srv)
    # assert
    assert task(store, "T-1").status == Status.CANCELLED
    assert not [e for e in events(store, "agent_wake", "T-1") if e.data.get("reason") == "ship_failed"]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_ship_with_several_unmerged_dependencies_is_error(make_project, start_server, calls, tmp_path):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "A")
    approved(srv, store, "B")
    send(srv, "A", "B")
    wait_ship(store, "B")
    approved(srv, store, "C", deps=["A", "B"])
    # act
    send(srv, "C")
    state = wait_ship(store, "C")
    # assert
    assert state["state"] == "error" and "несколько незавершённых зависимостей" in state["error"]
    assert task(store, "C").status == Status.APPROVED


def test_ship_base_is_repo_base_when_dependency_is_done(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "base.txt"
    store = make_project(ship={"command": "echo {base} > " + str(out)})
    srv = start_server()
    approved(srv, store, "A")
    approved(srv, store, "B", deps=["A"])
    srv.board_for("pay").transition("A", Status.DONE, "user")
    # act
    send(srv, "B")
    wait_ship(store, "B")
    # assert
    assert out.read_text().strip() == "main"


def test_ship_commits_leftover_changes_before_command(make_project, start_server, calls, git_repo, tmp_path):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "T-1")
    (Path(task(store, "T-1").worktree) / "late.txt").write_text("late")
    # act
    send(srv, "T-1")
    wait_ship(store, "T-1")
    # assert
    assert len(events(store, "task_committed", "T-1")) == 2
    assert "late.txt" in run_git(git_repo, "show", "--stat", "--format=%s", "T-1")


def test_ship_in_progress_is_marked_interrupted_after_restart(make_project, start_server, calls, tmp_path):
    # arrange
    pidfile = tmp_path / "ship.pid"
    store = make_project(ship={"command": "echo $$ > %s; echo partial; sleep 300" % pidfile})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_events(store, "ship_started", "T-1")
    wait_for(pidfile.exists, what="команда стартовала")
    pid = int(pidfile.read_text())
    # act
    srv = restart(srv, start_server)
    state = wait_ship(store, "T-1")
    # assert
    assert state["state"] == "interrupted" and "partial" in state["output_tail"]
    assert task(store, "T-1").status == Status.APPROVED
    assert not _alive(pid)
    assert len(events(store, "ship_started", "T-1")) == 1
    assert any("перезапуском" in t for t in thread_texts(store, "T-1"))


def test_ship_foreign_worktree_is_error_and_command_not_run(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "ran.txt"
    store = make_project(ship={"command": "touch " + str(out)})
    srv = start_server()
    approved(srv, store, "T-1")
    srv.board_for("pay").update_task("T-1", "system", {"worktree": str(tmp_path)})
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    # assert
    assert state["state"] == "error" and not out.exists()


def test_ship_success__card_saved__should_be_sent_with_ship_done_before_finish_event(make_project, start_server,
                                                                                     calls, monkeypatch):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "T-1")
    board = srv.board_for("pay")
    saved, at_finish = [], []
    save_task, record = board.store.save_task, board.record

    def recording_save(t):
        saved.append((t.status, (t.ship or {}).get("state")))
        save_task(t)

    def recording_record(type, data):
        if type == "ship_finished":
            t = board.store.load_tasks()["T-1"]
            at_finish.append((t.status, t.ship["state"]))
        return record(type, data)

    monkeypatch.setattr(board.store, "save_task", recording_save)
    monkeypatch.setattr(board, "record", recording_record)
    # act
    send(srv, "T-1")
    wait_events(store, "ship_finished", "T-1")
    # assert
    assert (Status.SENT, "running") not in saved
    assert saved[-1] == (Status.SENT, "done")
    assert at_finish == [(Status.SENT, "done")]


def test_ship_success_after_task_left_approved_is_error_with_full_state(make_project, start_server, calls, tmp_path):
    # arrange
    gate = tmp_path / "gate"
    store = make_project(ship={"command": "echo shipped; while [ ! -e %s ]; do sleep 0.05; done" % gate})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_events(store, "ship_started", "T-1")
    srv.board_for("pay").transition("T-1", Status.IN_PROGRESS, "user")
    # act
    gate.write_text("")
    state = wait_ship(store, "T-1")
    # assert
    assert state["state"] == "error" and state["exit_code"] == 0 and "shipped" in state["output_tail"]
    assert "не одобрена" in state["error"] and state["at"]
    assert task(store, "T-1").status == Status.IN_PROGRESS


def test_cancel_during_ship_stops_process_group_and_marks_cancelled(make_project, start_server, calls, tmp_path):
    # arrange
    pidfile = tmp_path / "p.pid"
    store = make_project(ship={"command": "sleep 300 & echo $! > %s; echo hello; wait" % pidfile})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_for(pidfile.exists, what="команда стартовала")
    pid = int(pidfile.read_text())
    # act
    srv.board_for("pay").transition("T-1", Status.CANCELLED, "user")
    state = wait_ship(store, "T-1", states=("cancelled",))
    # assert
    wait_for(lambda: not _alive(pid), what="группа остановлена")
    assert state["state"] == "cancelled" and task(store, "T-1").status == Status.CANCELLED


def test_sent_task_does_not_wake_agent_from_accumulated_reasons(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_for(lambda: task(store, "T-1").status == Status.SENT, what="sent")
    started = len(events(store, "turn_started", "T-1"))
    # act
    srv.board_for("pay").post_message("T-1", "user", "после отправки")
    runner_sync(srv)
    # assert
    assert len(events(store, "turn_started", "T-1")) == started


def test_queued_ship_after_restart_gets_not_started_text(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "T-1")
    srv.stop()
    srv.board_for("pay").update_task("T-1", "system", {"ship": {"state": "queued", "at": "x"}})
    # act
    start_server()
    state = wait_ship(store, "T-1")
    # assert
    assert state["state"] == "interrupted"
    assert any("не была запущена" in t for t in thread_texts(store, "T-1"))


def test_ship_posts_message_when_leftovers_committed(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "true"})
    srv = start_server()
    approved(srv, store, "T-1")
    (Path(task(store, "T-1").worktree) / "late.txt").write_text("late")
    # act
    send(srv, "T-1")
    wait_ship(store, "T-1")
    # assert
    assert any("закоммичены оставшиеся изменения" in t for t in thread_texts(store, "T-1"))


def test_ship_timeout_message_uses_timeout_of_the_run(make_project, start_server, calls):
    # arrange
    store = make_project(ship={"command": "sleep 300", "timeout": 1})
    srv = start_server()
    approved(srv, store, "T-1")
    send(srv, "T-1")
    wait_events(store, "ship_started", "T-1")
    srv.board_for("pay").update_config({"ship": {"command": "sleep 300", "timeout": 999}})
    # act
    wait_ship(store, "T-1")
    # assert
    assert any("(1 с)" in t for t in thread_texts(store, "T-1"))


def test_ship_finished_command_without_children__no_signal_to_its_group(make_project, start_server, calls,
                                                                        monkeypatch):
    # arrange: после сбора лидера пустая группа не получает сигналов — её номер мог занять чужой процесс
    store = make_project(ship={"command": "echo {key}"})
    srv = start_server()
    approved(srv, store, "T-1")
    started = []
    real_start = ship_module.start

    def start(*args, **kwargs):
        proc = real_start(*args, **kwargs)
        started.append(proc.pid)
        return proc

    real_killpg = os.killpg
    signals = []

    def killpg(pgid, sig):
        signals.append((pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(ship_module, "start", start)
    monkeypatch.setattr(os, "killpg", killpg)
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    runner_sync(srv)
    # assert
    assert state["state"] == "done" and started
    assert [sig for pgid, sig in signals if pgid == started[0] and sig != 0] == []


SHIP_SECRET = "ship-s3cret-TOKEN-42"


@pytest.mark.parametrize("exit_code", [0, 3])
def test_ship_output_is_scrubbed_of_tracker_secrets(make_project, start_server, calls, monkeypatch, exit_code):
    # arrange: команда печатает токен трекера
    monkeypatch.setenv("AB_SHIP_JIRA_TOKEN", SHIP_SECRET)
    store = make_project(
        ship={"command": "echo before; echo token=$AB_SHIP_JIRA_TOKEN; echo after; exit %d" % exit_code},
        tracker={"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
                 "auth": {"type": "token_env", "env": "AB_SHIP_JIRA_TOKEN"}})
    srv = start_server()
    approved(srv, store, "T-1")
    # act
    send(srv, "T-1")
    state = wait_ship(store, "T-1")
    runner_sync(srv)
    # assert
    log = (store.root / "logs" / "T-1.ship.log").read_text()
    assert "token=***" in log and "before" in log and "after" in log
    assert "token=***" in state["output_tail"]
    texts = "\n".join(thread_texts(store, "T-1"))
    assert SHIP_SECRET not in log + state["output_tail"] + texts
    if exit_code:
        assert "token=***" in texts
    assert SHIP_SECRET not in "\n".join(p.read_text(errors="replace") for p in store.root.rglob("*") if p.is_file())


# --- отправка в ревью сразу после одобрения ---

def test_on_approve__approve_button__commits_then_ships_with_tracker_values(make_project, start_server, calls,
                                                                           tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(ship={"on_approve": True, "command": "printf '%s|%s|%s|%s\\n' {tracker_key} {tracker_url} "
                                                              "\"$AGENT_BOARD_TRACKER_KEY\" \"$AGENT_BOARD_TRACKER_URL\" > "
                                                              + str(out)})
    srv = start_server()
    add(srv, "T-1", script({"write": {"path": "f.txt", "text": "x"}}, {"cli": ["status", "T-1", "in_review"]}))
    srv.board_for("pay").update_task("T-1", "user", {"tracker_key": "PROJ-7",
                                                     "tracker_url": "https://tracker.example.com/PROJ-7"})
    wait_events(store, "turn_finished", "T-1")
    # act
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    state = wait_ship(store, "T-1")
    # assert
    assert state["state"] == "done" and task(store, "T-1").status == Status.SENT
    assert out.read_text().strip() == "PROJ-7|https://tracker.example.com/PROJ-7|PROJ-7|https://tracker.example.com/PROJ-7"
    assert events(store, "task_committed", "T-1")[0].seq < events(store, "ship_started", "T-1")[0].seq
    assert [e.data for e in events(store, "ship_requested")] == [{"keys": ["T-1"], "auto": True}]


def test_on_approve__no_tracker_link__placeholders_and_env_are_empty(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(ship={"on_approve": True, "command": "printf '[%s][%s][%s][%s]' {tracker_key} {tracker_url} "
                                                              "\"$AGENT_BOARD_TRACKER_KEY\" \"$AGENT_BOARD_TRACKER_URL\" > "
                                                              + str(out)})
    srv = start_server()
    # act
    approved(srv, store, "T-1")
    wait_ship(store, "T-1")
    # assert
    assert out.read_text() == "[][][][]"


def test_on_approve__disabled__approval_does_not_ship(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(ship={"command": "touch " + str(out), "on_approve": False})
    srv = start_server()
    # act
    approved(srv, store, "T-1")
    runner_sync(srv)
    # assert
    assert task(store, "T-1").status == Status.APPROVED and task(store, "T-1").ship is None
    assert not events(store, "ship_requested") and not out.exists()


def test_on_approve__review_round_approval__ships(make_project, start_server, calls, crit_bin, tmp_path):
    # arrange
    store = review_project(make_project, crit_bin, ship={"command": "true", "on_approve": True})
    srv = start_server()
    to_review(srv, store)
    srv.board_for("pay").request_review("A", "user")
    first = wait_round(store, 1)
    # act
    fake_crit.finish(crit_dir(store), first["session"])
    state = wait_ship(store, "A")
    # assert
    assert state["state"] == "done" and task(store, "A").status == Status.SENT


def test_on_approve__commit_fails__no_ship(make_project, start_server, calls, tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(ship={"command": "touch " + str(out), "on_approve": True})
    srv = start_server()
    add(srv, "A", script({"write": {"path": "x.txt", "text": "<<<<<<< ours\nmine\n"}}, {"cli": ["status", "A", "in_review"]}))
    wait_events(store, "turn_finished", "A")
    # act
    srv.board_for("pay").transition("A", Status.APPROVED, "user")
    wait_for(lambda: task(store, "A").status == Status.IN_REVIEW, what="A вернулась на ревью")
    runner_sync(srv)
    # assert
    assert not events(store, "ship_requested") and task(store, "A").ship is None and not out.exists()


def test_on_approve__dependency_still_shipping__dependent_goes_after_it(make_project, start_server, calls, tmp_path):
    # arrange: отправка A ждёт флага, пока одобряется B
    flag, out = tmp_path / "go", tmp_path / "order.txt"
    store = make_project(ship={"on_approve": True, "command": "if [ {key} = A ]; then while [ ! -e %s ]; do sleep 0.05; "
                                                              "done; fi; echo {key} {base} >> %s" % (flag, out)})
    srv = start_server()
    approved(srv, store, "A")
    wait_ship(store, "A", states=("running",))
    # act
    approved(srv, store, "B", deps=["A"])
    wait_ship(store, "B", states=("queued",))
    runner_sync(srv)
    flag.write_text("")
    wait_ship(store, "B")
    # assert
    assert out.read_text().splitlines() == ["A main", "B A"]
    assert task(store, "A").status == task(store, "B").status == Status.SENT


def test_on_approve__dependency_ship_fails__dependent_skipped(make_project, start_server, calls, tmp_path):
    # arrange
    flag = tmp_path / "go"
    store = make_project(ship={"on_approve": True, "command": "if [ {key} = A ]; then while [ ! -e %s ]; do sleep 0.05; "
                                                              "done; exit 3; fi" % flag})
    srv = start_server()
    approved(srv, store, "A")
    wait_ship(store, "A", states=("running",))
    approved(srv, store, "B", deps=["A"])
    wait_ship(store, "B", states=("queued",))
    runner_sync(srv)
    # act
    flag.write_text("")
    state = wait_ship(store, "B")
    # assert
    assert task(store, "A").ship["state"] == "error"
    assert state["state"] == "skipped" and "A" in state["reason"] and task(store, "B").status == Status.APPROVED


def test_on_approve__dependency_approved_but_not_shipped__error_in_thread(make_project, start_server, calls, tmp_path):
    # arrange: A одобрена, пока автоотправка была выключена
    out = tmp_path / "out.txt"
    store = make_project(ship={"command": "echo {key} >> " + str(out)})
    srv = start_server()
    approved(srv, store, "A")
    srv.board_for("pay").update_config({"ship": {"command": "echo {key} >> " + str(out), "on_approve": True}})
    # act
    approved(srv, store, "B", deps=["A"])
    state = wait_ship(store, "B", states=("error",))
    runner_sync(srv)
    # assert
    assert "A" in state["error"] and task(store, "B").status == Status.APPROVED
    assert any("Автоматическая отправка" in t and "A" in t for t in thread_texts(store, "B"))
    assert not out.exists()


# --- уборка: логи ревью и данные crit закрытых задач ---

def test_review_round__large_log__trimmed_to_limit(make_project, start_server, calls, crit_bin, monkeypatch):
    # arrange
    from agent_board import review as review_mod

    monkeypatch.setattr(review_mod, "LOG_LIMIT_BYTES", 4096)
    store = review_project(make_project, crit_bin)
    srv = start_server()
    to_review(srv, store)
    log = store.root / "logs" / "A.review.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("старый раунд\n" * 2000, encoding="utf-8")
    # act
    srv.board_for("pay").request_review("A", "user")
    wait_round(store, 1)
    # assert
    assert log.stat().st_size < 4096 + 2048


def _closed_long_ago(store, key, session):
    from agent_board.board import Board

    old = Board(store, clock=lambda: "2020-01-01T00:00:00+00:00")
    old.add_task(new_task_from_dict({"key": key, "title": key}), "user")
    old.update_task(key, "system", {"review": {"state": "approved", "session": session, "round": 1}})
    old.transition(key, Status.CANCELLED, "user")
    data = store.root / "crit" / "reviews" / session
    data.mkdir(parents=True)
    return data


def test_server_start__review_data_of_long_closed_task__removed(make_project, start_server, calls):
    # arrange
    store = make_project()
    data = _closed_long_ago(store, "A", "aaaaaaaaaaaa")
    # act
    srv = start_server()
    runner_sync(srv)
    # assert
    wait_for(lambda: not data.exists(), what="данные ревью удалены")


def test_runner__daily_housekeeping__removes_review_data(make_project, start_server, calls, monkeypatch):
    # arrange
    monkeypatch.setattr(runner_mod, "HOUSEKEEPING_SECONDS", 0.2)
    store = make_project()
    srv = start_server()
    runner_sync(srv)
    # act
    data = _closed_long_ago(srv.board_for("pay").store, "B", "bbbbbbbbbbbb")
    # assert
    wait_for(lambda: not data.exists(), what="данные ревью удалены ежедневной уборкой")


# --- повтор упавшего хода пользователем ---

RETRY_NOTICE = "Your previous turn broke off"


def test_retry_after_failed_first_turn_starts_new_session_and_resets_failures(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"fail": "boom"}))
    wait_events(store, "turn_failed", "T-1", count=3)
    runner_sync(srv)
    # act
    srv.board_for("pay").retry("T-1", "user")
    # assert: после повтора снова три попытки — счётчик неудач сброшен
    wait_events(store, "turn_failed", "T-1", count=6)
    runner_sync(srv)
    retried = calls[3]
    assert retried["kind"] == "start" and retried["session"] not in {c["session"] for c in calls[:3]}
    assert RETRY_NOTICE in retried["text"] and "boom" in retried["text"]
    assert len(events(store, "turn_started", "T-1")) == 6
    assert any("agent-board retry T-1" in t for t in thread_texts(store, "T-1"))


def test_retry_resumes_same_session_with_notice_and_undelivered_reasons(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    board = srv.board_for("pay")
    add(srv, "T-1")
    wait_events(store, "turn_finished", "T-1")
    session = task(store, "T-1").session_id
    board.post_message("T-1", "user", "доделай " + script({"fail": "обрыв соединения"}))
    wait_events(store, "turn_failed", "T-1", count=3)
    runner_sync(srv)
    # act
    board.retry("T-1", "user")
    wait_events(store, "turn_failed", "T-1", count=4)
    runner_sync(srv)
    # assert
    retried = calls[4]
    assert (retried["kind"], retried["session"]) == ("resume", session)
    assert RETRY_NOTICE in retried["text"] and "обрыв соединения" in retried["text"]
    assert retried["text"].index(RETRY_NOTICE) < retried["text"].index("доделай")
    assert retried["text"].count(RETRY_NOTICE) == 1
    wake = events(store, "agent_wake", "T-1")[-1].data
    assert (wake["reason"], wake["error"]) == ("retry", "обрыв соединения")


def test_retry_of_project_agent_resumes_its_session(make_project, start_server, calls):
    # arrange
    store = make_project(project_session_id="p-1")
    srv = start_server()
    board = srv.board_for("pay")
    board.post_message("_project", "user", "разбери " + script({"fail": "упал CLI"}))
    wait_events(store, "turn_failed", "_project", count=3)
    runner_sync(srv)
    # act
    board.retry("_project", "user")
    wait_events(store, "turn_started", "_project", count=4)
    wait_events(store, "turn_failed", "_project", count=4)
    # assert
    retried = calls[3]
    assert (retried["kind"], retried["session"]) == ("resume", "p-1")
    assert RETRY_NOTICE in retried["text"] and "упал CLI" in retried["text"] and "разбери" in retried["text"]


def test_retry_of_running_agent_is_rejected_without_second_turn(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    add(srv, "T-1", script({"sleep": 20}))
    wait_for(lambda: task(store, "T-1").agent_state == "running", what="ход T-1 идёт")
    # act
    with pytest.raises(ValueError, match="running"):
        srv.board_for("pay").retry("T-1", "user")
    runner_sync(srv)
    # assert
    assert len(calls) == 1 and len(events(store, "turn_started", "T-1")) == 1


# --- автоодобрение по настройке auto_approve ---

def test_auto_approve__agent_moves_to_review__commits_and_ships_without_user(make_project, start_server, calls,
                                                                              git_repo, tmp_path):
    # arrange
    out = tmp_path / "out.txt"
    store = make_project(auto_approve=True, ship={"command": "echo {key} > " + str(out), "on_approve": True})
    srv = start_server()
    # act
    add(srv, "A", script({"write": {"path": "a-file.txt", "text": "from A"}}, {"cli": ["status", "A", "in_review"]}))
    state = wait_ship(store, "A")
    # assert
    committed = events(store, "task_committed", "A")
    assert state["state"] == "done" and task(store, "A").status == Status.SENT
    assert run_git(git_repo, "show", "A:a-file.txt") == "from A"
    assert out.read_text().strip() == "A"
    approvals = [e.data for e in events(store, "status_changed", "A") if e.data["to"] == "approved"]
    assert approvals == [{"key": "A", "from": "in_review", "to": "approved", "actor": "system", "auto": True}]
    assert committed[0].seq < events(store, "ship_started", "A")[0].seq
    assert "Одобрено автоматически (auto_approve)" in thread_texts(store, "A")
    assert task(store, "A").review is None


def test_auto_approve__disabled__task_waits_in_review(make_project, start_server, calls):
    # arrange
    store = make_project()
    srv = start_server()
    # act
    add(srv, "A", script({"write": {"path": "a.txt", "text": "A"}}, {"cli": ["status", "A", "in_review"]}))
    wait_events(store, "turn_finished", "A")
    runner_sync(srv)
    # assert
    assert task(store, "A").status == Status.IN_REVIEW and not events(store, "task_committed", "A")


def test_auto_approve__research_task__waits_in_review(make_project, start_server, calls):
    # arrange
    store = make_project(auto_approve=True)
    srv = start_server()
    # act
    add(srv, "R", script({"write": {"path": "reports/r.md", "text": "отчёт"}}, {"cli": ["status", "R", "in_review"]}),
        kind="research")
    wait_events(store, "turn_finished", "R")
    runner_sync(srv)
    # assert
    assert task(store, "R").status == Status.IN_REVIEW and not events(store, "task_committed", "R")


def test_auto_approve__commit_fails__task_back_in_review_with_message(make_project, start_server, calls):
    # arrange
    store = make_project(auto_approve=True)
    srv = start_server()
    # act
    add(srv, "A", script({"write": {"path": "x.txt", "text": "<<<<<<< ours\nmine\n"}}, {"cli": ["status", "A", "in_review"]}))
    wait_for(lambda: any("x.txt" in t for t in thread_texts(store, "A")), what="сообщение о неудачном коммите")
    runner_sync(srv)
    # assert
    assert task(store, "A").status == Status.IN_REVIEW
    assert not events(store, "task_committed", "A")
    assert len([e for e in events(store, "status_changed", "A") if e.data["to"] == "approved"]) == 1


# --- приоритет в очереди ---

def started_order(store, keys):
    started = [e.data["key"] for e in events(store, "turn_started") if e.data.get("key") in keys]
    return started


def test_queue__full_slots__high_starts_before_normal_and_low_after(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 1}))
    wait_events(store, "turn_started", "A")
    # act
    for key, priority in (("L", "low"), ("N1", "normal"), ("H", "high"), ("N2", "normal")):
        add(srv, key, priority=priority)
        wait_for(lambda: task(store, key).agent_state == "waiting_turn", what="%s в очереди" % key)
    wait_events(store, "turn_finished", "L")
    # assert
    assert started_order(store, {"L", "N1", "H", "N2"}) == ["H", "N1", "N2", "L"]


def test_queue__priority_raised_while_waiting__moves_ahead(make_project, start_server, calls):
    # arrange
    store = make_project(max_agents=1)
    srv = start_server()
    add(srv, "A", script({"sleep": 1}))
    wait_events(store, "turn_started", "A")
    for key in ("B", "C"):
        add(srv, key)
        wait_for(lambda: task(store, key).agent_state == "waiting_turn", what="%s в очереди" % key)
    # act
    srv.board_for("pay").update_task("C", "project_agent", {"priority": "high"})
    wait_events(store, "turn_finished", "B")
    # assert
    assert started_order(store, {"B", "C"}) == ["C", "B"]
    assert events(store, "turn_finished", "A"), "идущий ход не прерывается"
    assert not events(store, "turn_failed", "A")
