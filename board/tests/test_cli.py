from __future__ import annotations

import json
import os
import sys

import pytest

from agent_board.cli import build_parser, main
from agent_board.model import Status
from agent_board.server import BoardServer


@pytest.fixture
def srv(tmp_home):
    assert main(["init", "pay", "--title", "Платежи"]) == 0
    server = BoardServer(port=0)
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_PROJECT", "pay")
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)


def run(capsys, *argv):
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv)
    assert code == 0, err
    return json.loads(out)


def test_init_add_task_show(srv, capsys):
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    task = run_json(capsys, "show", "T-1")
    assert task["key"] == "T-1" and task["title"] == "Первая"
    assert run_json(capsys, "state")["tasks"][0]["key"] == "T-1"


def test_init_existing_project_fails(tmp_home, capsys):
    assert run(capsys, "init", "pay", "--title", "A")[0] == 0
    code, _, err = run(capsys, "init", "pay", "--title", "A")
    assert code == 1 and err


def test_ask_sets_waiting_answer_and_prints_id(srv, capsys):
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    run_json(capsys, "status", "T-1", "in_progress")
    out = run_json(capsys, "ask", "T-1", "--text", "Как?", "--option", "A", "--option", "B", "--recommended", "A")
    assert out["question_id"] == "q1"
    assert run_json(capsys, "show", "T-1")["status"] == "waiting_answer"


def test_agent_cannot_mark_done(srv, capsys):
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    code, out, err = run(capsys, "status", "T-1", "done")
    assert code == 1 and err.strip() and out == ""


def test_update_sends_only_passed_fields(srv, capsys):
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    run_json(capsys, "update", "T-1", "--summary", "готово", "--todo-json", '[{"text": "a", "done": true}]')
    task = run_json(capsys, "show", "T-1")
    assert task["summary"] == "готово" and task["todo"][0]["text"] == "a" and task["branch"] is None


def test_update_bad_json_exits_1_before_request(tmp_home, capsys):
    code, _, err = run(capsys, "update", "T-1", "--todo-json", "{oops")
    assert code == 1 and "JSON" in err


def test_say_project_agent_to_project_thread(srv, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    run_json(capsys, "say", "_project", "--text", "привет")
    msgs = run_json(capsys, "thread", "_project")["messages"]
    assert msgs[0]["author"] == "project_agent" and msgs[0]["text"] == "привет"
    assert run_json(capsys, "state")["project_unread"] == 1


def test_propose_and_events(srv, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    out = run_json(capsys, "propose", "add_task", "--json", '{"task": {"key": "T-9", "title": "n"}}', "--reason", "надо")
    assert out["proposal_id"] == "p1"
    events = run_json(capsys, "events", "--since", "0")["events"]
    assert any(e["type"] == "proposal_created" for e in events)


def test_no_server_exits_2(tmp_home, capsys):
    code, _, err = run(capsys, "state")
    assert code == 2 and "доска не запущена: python3 -m agent_board serve" in err


def test_missing_project_exits_1(srv, capsys, monkeypatch):
    monkeypatch.delenv("AGENT_BOARD_PROJECT")
    code, _, err = run(capsys, "state")
    assert code == 1 and "проект" in err


def test_serve_when_already_running_exits_3(srv, capsys):
    # act
    code, out, err = run(capsys, "serve")

    # assert
    assert code == 3 and "уже запущена" in err and out == ""
    assert run_json(capsys, "state")["config"]["slug"] == "pay"


def test_serve_port_defaults_to_stable_selection():
    # act
    args = build_parser().parse_args(["serve"])
    forced = build_parser().parse_args(["serve", "--port", "9000"])

    # assert
    assert args.port is None and forced.port == 9000


def test_say_from_task_env_escalates_to_project_agent(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")

    # act
    out = run_json(capsys, "say", "_project", "--text", "нужна помощь")

    # assert
    wakes = [e["data"] for e in out["events"] if e["type"] == "agent_wake"]
    assert wakes == [{"key": "_project", "reason": "escalation", "from": "T-1", "by": "agent"}]


def test_say_without_task_env_does_not_escalate(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)

    # act
    out = run_json(capsys, "say", "_project", "--text", "просто так")

    # assert
    assert [e["type"] for e in out["events"]] == ["message_posted"]



# --- lock-run ---

def _declare_resource(srv):
    from agent_board.store import ProjectStore

    store = ProjectStore("pay")
    config = store.load_config()
    config.resources = [{"name": "tests", "description": "функциональные тесты"}]
    store.save_config(config)


def _lock_events(type):
    from agent_board.store import ProjectStore

    return [e.data for e in ProjectStore("pay").read_events() if e.type == type]


def test_lock_run_runs_command_and_releases(srv, capsys, monkeypatch, tmp_path):
    # arrange
    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")
    marker = tmp_path / "ran"
    # act
    code, _, _ = run(capsys, "lock-run", "tests", "--", sys.executable, "-c", "open(%r, 'w').write('x')" % str(marker))
    # assert
    assert code == 0 and marker.read_text() == "x"
    assert _lock_events("lock_acquired") == [{"key": "T-1", "resource": "tests"}]
    assert _lock_events("lock_released") == [{"key": "T-1", "resource": "tests"}]


def test_lock_run_returns_command_exit_code_and_still_releases(srv, capsys, monkeypatch):
    # arrange
    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")
    # act
    code, _, _ = run(capsys, "lock-run", "tests", "--", sys.executable, "-c", "raise SystemExit(7)")
    # assert
    assert code == 7
    assert len(_lock_events("lock_released")) == 1


def test_lock_run_waits_for_holder_and_task_is_queued(srv, capsys, monkeypatch):
    # arrange
    import threading
    import time

    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    run_json(capsys, "add-task", "--json", '{"key": "T-2", "title": "y"}')
    queue = srv.hub.queue("pay")
    assert queue.acquire("tests", "T-1", os.getpid()) == 0
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-2")
    monkeypatch.setenv("AGENT_BOARD_LOCK_POLL", "0.05")
    result = {}
    worker = threading.Thread(target=lambda: result.update(code=main(["lock-run", "tests", "--", sys.executable, "-c", "pass"])))
    # act
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and not _lock_events("lock_waiting"):
        time.sleep(0.02)
    waiting_status = srv.hub.board("pay").store.load_tasks()["T-2"].status.value
    queue.release("tests", "T-1", os.getpid())
    worker.join(10)
    # assert
    assert not worker.is_alive() and result["code"] == 0
    assert waiting_status == "queued"
    assert srv.hub.board("pay").store.load_tasks()["T-2"].status.value == "not_started"
    assert "позиция 1" in capsys.readouterr().err


def test_lock_run_without_task_env_fails(srv, capsys):
    # arrange
    _declare_resource(srv)
    # act
    code, _, err = run(capsys, "lock-run", "tests", "--", "true")
    # assert
    assert code == 1 and "AGENT_BOARD_TASK" in err


def test_lock_run_undeclared_resource_fails(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")
    # act
    code, _, err = run(capsys, "lock-run", "stand", "--", "true")
    # assert
    assert code == 1 and "stand" in err


def test_lock_run_keyboard_interrupt_releases_lock(srv, capsys, monkeypatch):
    # arrange
    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")

    class Interrupted:
        calls = 0

        def wait(self, timeout=None):
            Interrupted.calls += 1
            if Interrupted.calls == 1:
                raise KeyboardInterrupt
            return -15

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr("agent_board.cli.subprocess.Popen", lambda *a, **k: Interrupted())
    # act
    code, _, _ = run(capsys, "lock-run", "tests", "--", "whatever")
    # assert
    assert code == 130
    assert len(_lock_events("lock_released")) == 1


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_lock_run_signal_kills_child_and_releases(srv, capsys, monkeypatch, tmp_path, sig):
    # arrange
    import signal
    import subprocess
    import time
    from pathlib import Path

    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    pidfile = tmp_path / "child.pid"
    env = dict(os.environ, AGENT_BOARD_TASK="T-1", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = "import os, time; open(%r, 'w').write(str(os.getpid())); time.sleep(30)" % str(pidfile)
    proc = subprocess.Popen([sys.executable, "-m", "agent_board", "lock-run", "tests", "--", sys.executable, "-c", child], env=env)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not (pidfile.exists() and pidfile.read_text()):
            time.sleep(0.02)
        child_pid = int(pidfile.read_text())
        # act
        proc.send_signal(getattr(signal, sig))
        proc.wait(10)
        # assert
        deadline = time.time() + 5
        alive = True
        while alive and time.time() < deadline:
            try:
                os.kill(child_pid, 0)
                time.sleep(0.02)
            except ProcessLookupError:
                alive = False
        assert not alive
        assert len(_lock_events("lock_released")) == 1
    finally:
        if proc.poll() is None:
            proc.kill()


def test_lock_run_waits_quietly_for_unreachable_board_once(srv, capsys, monkeypatch):
    # arrange
    from agent_board import cli

    _declare_resource(srv)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")
    monkeypatch.setenv("AGENT_BOARD_LOCK_POLL", "0.01")
    real = cli._request
    state = {"fails": 3}

    def flaky(method, path, body=None):
        if state["fails"] > 0 and path.endswith("/acquire"):
            state["fails"] -= 1
            raise cli.CliError(cli.NOT_RUNNING, 2)
        return real(method, path, body)

    monkeypatch.setattr(cli, "_request", flaky)
    # act
    code, _, err = run(capsys, "lock-run", "tests", "--", sys.executable, "-c", "pass")
    # assert
    assert code == 0 and err.count("доска недоступна") == 1


def test_lock_run_undeclared_resource_prints_single_error(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_TASK", "T-1")
    # act
    code, _, err = run(capsys, "lock-run", "stand", "--", "true")
    # assert
    assert code == 1 and len(err.strip().splitlines()) == 1


def test_stage_with_empty_actor_env_is_rejected_as_agent(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "")
    # act
    code, _, err = run(capsys, "stage", "work")
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "state")["config"]["stage"] == "decomposition"


def test_stage_changed_by_user_but_not_by_agent(srv, capsys, monkeypatch):
    # act
    out = run_json(capsys, "stage", "work")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    code, _, err = run(capsys, "stage", "done")
    # assert
    assert [e["type"] for e in out["events"]] == ["stage_changed"]
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "state")["config"]["stage"] == "work"


def test_update_rule_violations_json_by_project_agent(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    # act
    run_json(capsys, "update", "T-1", "--rule-violations-json", '[{"rule": "r", "text": "t"}]')
    # assert
    assert run_json(capsys, "show", "T-1")["rule_violations"] == [{"rule": "r", "text": "t"}]


def test_update_has_no_review_option():
    # arrange / act / assert: состояние ревью ведёт раннер, агент его не задаёт
    with pytest.raises(SystemExit):
        build_parser().parse_args(["update", "T-1", "--review-json", "{}"])


def test_ship_sends_request_as_user_even_with_agent_actor_env(srv, capsys, monkeypatch):
    # arrange
    srv.board_for("pay").update_config({"ship": {"command": "true"}})
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").transition("T-1", Status.IN_REVIEW, "agent")
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    # act
    result = run_json(capsys, "ship", "T-1")
    # assert
    assert [e["type"] for e in result["events"]] == ["ship_requested"]


def test_ship_without_command_exits_1_with_reason(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    # act
    srv.board_for("pay").transition("T-1", Status.IN_REVIEW, "agent")
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    code, _, err = run(capsys, "ship", "T-1")
    # assert
    assert code != 0 and "команда отправки" in err


@pytest.fixture
def fake_jira(monkeypatch):
    from fakes.fake_jira import FakeJira

    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIRA_TOKEN", "cli-token-value")
    fake = FakeJira(authorization="Bearer cli-token-value").start()
    yield fake
    fake.stop()


def with_tracker(srv, jira):
    srv.board_for("pay").update_config({"tracker": {
        "type": "jira", "url": jira.url, "project": "MNZ", "issue_type": "Task",
        "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}})


def test_tracker_create_and_check_as_user(srv, capsys, fake_jira):
    # arrange
    with_tracker(srv, fake_jira)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").set_stage("tracker", "user")
    # act
    created = run_json(capsys, "tracker", "create")
    checked = run_json(capsys, "tracker", "check")
    # assert
    assert created["created"][0]["tracker_key"] == "MNZ-1" and checked == {"ok": True}


def test_tracker_errors_exit_1(srv, capsys, fake_jira):
    # arrange
    with_tracker(srv, fake_jira)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").set_stage("tracker", "user")
    fake_jira.fail_all = (401, None)
    # act
    created = run(capsys, "tracker", "create")
    checked = run(capsys, "tracker", "check")
    # assert
    assert created[0] == 1 and "JIRA_TOKEN" in created[2]
    assert checked[0] == 1 and "авторизац" in checked[2]


def test_request_read_timeout_says_request_still_runs(tmp_home):
    # arrange: доска приняла соединение, но не отвечает
    import socket

    from agent_board import cli
    from agent_board.store import atomic_write_json, board_home

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        board_home().mkdir(parents=True, exist_ok=True)
        atomic_write_json(board_home() / "server.json",
                          {"port": listener.getsockname()[1], "token": "t", "pid": os.getpid()})
        # act
        with pytest.raises(cli.CliError) as info:
            cli._request("POST", "/api/p/pay/tracker/create", {"actor": "user"}, timeout=0.3)
    finally:
        listener.close()
    # assert
    assert info.value.code == 2 and "всё ещё выполняется" in str(info.value)
