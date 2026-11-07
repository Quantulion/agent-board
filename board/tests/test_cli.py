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


def test_propose__user_actions__accepted_and_not_applied(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")

    # act
    out = run_json(capsys, "propose", "stage", "--json", '{"stage": "work"}', "--reason", "пользователь просит")
    cfg = run_json(capsys, "propose", "set_config", "--json", '{"fields": {"max_agents": 4}}')

    # assert
    assert (out["proposal_id"], cfg["proposal_id"]) == ("p1", "p2")
    state = run_json(capsys, "state")
    assert state["config"]["stage"] == "decomposition" and state["config"]["max_agents"] != 4
    code, _, _ = run(capsys, "stage", "work")
    assert code == 1


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
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")

    # act
    out = run_json(capsys, "say", "_project", "--text", "нужна помощь")

    # assert
    wakes = [e["data"] for e in out["events"] if e["type"] == "agent_wake"]
    assert wakes == [{"key": "_project", "reason": "escalation", "from": "T-1", "by": "agent"}]


def test_say_without_task_env_does_not_escalate(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")

    # act
    out = run_json(capsys, "say", "_project", "--text", "просто так")

    # assert
    assert [e["type"] for e in out["events"]] == ["message_posted"]


def test_say_without_actor_env_posts_as_user(srv, capsys, monkeypatch):
    # arrange: человек в терминале
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)

    # act
    run_json(capsys, "say", "_project", "--text", "от пользователя")

    # assert
    msgs = run_json(capsys, "thread", "_project")["messages"]
    assert msgs[-1]["author"] == "user" and msgs[-1]["text"] == "от пользователя"


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
            raise cli.CliError(cli.msg("cli.not_running"), 2)
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


def test_update__priority_by_project_agent__saved(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    # act
    out = run_json(capsys, "update", "T-1", "--priority", "high")
    # assert
    assert "priority_changed" in [e["type"] for e in out["events"]]
    assert run_json(capsys, "show", "T-1")["priority"] == "high"


def test_update__priority_by_task_agent__rejected(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    # act
    code, _, err = run(capsys, "update", "T-1", "--priority", "high")
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "show", "T-1")["priority"] == "normal"


def test_update_has_no_review_option():
    # arrange / act / assert: состояние ревью ведёт раннер, агент его не задаёт
    with pytest.raises(SystemExit):
        build_parser().parse_args(["update", "T-1", "--review-json", "{}"])


@pytest.mark.parametrize("actor_env", ["agent", "project_agent", ""])
def test_ship_with_agent_actor_env_is_rejected(srv, capsys, monkeypatch, actor_env):
    # arrange
    srv.board_for("pay").update_config({"ship": {"command": "true"}})
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").transition("T-1", Status.IN_REVIEW, "agent")
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor_env)
    # act
    code, _, err = run(capsys, "ship", "T-1")
    # assert
    assert code == 1 and "пользователь" in err
    assert srv.board_for("pay").store.load_tasks()["T-1"].ship is None


def test_ship_sends_request_as_user_without_actor_env(srv, capsys):
    # arrange
    srv.board_for("pay").update_config({"ship": {"command": "true"}})
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").transition("T-1", Status.IN_REVIEW, "agent")
    srv.board_for("pay").transition("T-1", Status.APPROVED, "user")
    # act
    result = run_json(capsys, "ship", "T-1")
    # assert
    assert [e["type"] for e in result["events"]] == ["ship_requested"]


def test_add_task_by_project_agent_is_rejected(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    # act
    code, _, err = run(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    # assert
    assert code == 1 and "предложения" in err
    assert srv.board_for("pay").store.load_tasks() == {}


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
        "type": "jira", "url": jira.url, "project": "PROJ", "issue_type": "Task",
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
    assert created["created"][0]["tracker_key"] == "PROJ-1" and checked == {"ok": True}


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


def test_tracker_sync_runs_one_pass_as_user(srv, capsys, fake_jira):
    # arrange
    with_tracker(srv, fake_jira)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").set_stage("tracker", "user")
    run_json(capsys, "tracker", "create")
    srv.board_for("pay").set_stage("work", "user")
    # act
    synced = run_json(capsys, "tracker", "sync")
    # assert
    assert synced["ok"] is True and synced["paused"] is False


def test_tracker_sync_failure_exits_1(srv, capsys, fake_jira):
    # arrange
    with_tracker(srv, fake_jira)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").set_stage("tracker", "user")
    run_json(capsys, "tracker", "create")
    srv.board_for("pay").set_stage("work", "user")
    fake_jira.fail_all = (401, None)
    # act
    code, _, err = run(capsys, "tracker", "sync")
    # assert
    assert code == 1 and "JIRA_TOKEN" in err


@pytest.mark.parametrize("action", ["create", "check", "sync"])
@pytest.mark.parametrize("actor_env", ["agent", "project_agent", ""])
def test_tracker_with_agent_actor_env_is_rejected(srv, capsys, monkeypatch, fake_jira, action, actor_env):
    # arrange
    with_tracker(srv, fake_jira)
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    srv.board_for("pay").set_stage("tracker", "user")
    if action == "sync":
        srv.board_for("pay").set_stage("work", "user")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor_env)
    # act
    code, _, err = run(capsys, "tracker", action)
    # assert
    assert code == 1 and "пользователь" in err
    assert fake_jira.requests == []


# --- настройки проекта ---

def test_config_get_json_prints_config(srv, capsys):
    # act
    config = run_json(capsys, "config", "get", "--json")
    # assert
    assert config["slug"] == "pay" and config["max_agents"] == 3


def test_config_get_prints_key_per_line(srv, capsys):
    # act
    code, out, _ = run(capsys, "config", "get")
    # assert
    assert code == 0 and "max_agents: 3" in out.splitlines() and 'tracker: {"type": "none"}' in out.splitlines()


def test_config_set_saves_and_dry_run_does_not(srv, capsys):
    # act
    dry = run_json(capsys, "config", "set", "--json", '{"max_agents": 2}', "--dry-run")
    after_dry = run_json(capsys, "config", "get", "--json")["max_agents"]
    saved = run_json(capsys, "config", "set", "--json", '{"max_agents": 2}')
    # assert
    assert dry["saved"] is False and dry["config"]["max_agents"] == 2 and after_dry == 3
    assert saved["saved"] is True and run_json(capsys, "config", "get", "--json")["max_agents"] == 2


def test_config_set_invalid_exits_1_with_reason(srv, capsys):
    # act
    code, _, err = run(capsys, "config", "set", "--json", '{"ship": {"command": "push \\"{branch}\\""}}')
    # assert
    assert code == 1 and "кавычки" in err


def test_config_set_bad_json_exits_1(srv, capsys):
    # act
    code, _, err = run(capsys, "config", "set", "--json", "{")
    # assert
    assert code == 1 and "JSON" in err


def test_config_validate_ok_and_broken_file(srv, capsys):
    # arrange
    ok = run_json(capsys, "config", "validate")
    store = srv.board_for("pay").store
    cfg = store.load_config()
    cfg.max_agents = 0
    store.save_config(cfg)
    # act
    code, _, err = run(capsys, "config", "validate")
    # assert
    assert ok["saved"] is False and code == 1 and "max_agents" in err


@pytest.mark.parametrize("argv", [["config", "get"], ["config", "set", "--json", '{"max_agents": 1}'],
                                  ["config", "validate"]])
@pytest.mark.parametrize("actor_env", ["agent", "project_agent", ""])
def test_config_with_agent_actor_env_is_rejected(srv, capsys, monkeypatch, argv, actor_env):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor_env)
    # act
    code, _, err = run(capsys, *argv)
    # assert
    assert code == 1 and "пользователь" in err
    assert srv.board_for("pay").store.load_config().max_agents == 3


def test_version__prints_package_version(tmp_home, capsys):
    # act
    code, out, _ = run(capsys, "version")
    # assert
    from agent_board import __version__

    assert code == 0 and out.strip() == __version__


def test_status_without_arguments__running_server__reports_project(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    # act
    report = run_json(capsys, "status", "--json")
    # assert
    assert report["server"]["running"] is True and report["server"]["pid"] == os.getpid()
    assert report["projects"] == [{"slug": "pay", "title": "Платежи", "stage": "decomposition", "tasks": 1}]


def test_server_discovery__contains_version(srv):
    # arrange
    from agent_board import __version__
    from agent_board.server import read_discovery

    # assert
    assert read_discovery()["version"] == __version__


# --- схема настроек ---

def _schema(capsys, monkeypatch):
    monkeypatch.delenv("AGENT_BOARD_PROJECT", raising=False)
    return {f["name"]: f for f in run_json(capsys, "config", "schema", "--json")["fields"]}


def _config_with(**fields):
    from agent_board.config import ProjectConfig

    return ProjectConfig(slug="pay", title="Платежи", **fields)


def _rejected(check, config) -> bool:
    try:
        check(config)
    except ValueError:
        return True
    return False


def test_config_schema__no_server_no_project__lists_every_settable_field(tmp_home, capsys, monkeypatch):
    # arrange
    from dataclasses import fields

    from agent_board.config import NOT_SETTABLE, ProjectConfig

    # act
    schema = _schema(capsys, monkeypatch)
    # assert
    assert set(schema) == {f.name for f in fields(ProjectConfig)} - set(NOT_SETTABLE)


def test_config_schema__fields__have_type_english_description_and_real_default(tmp_home, capsys, monkeypatch):
    # arrange
    default = _config_with()
    # act
    schema = _schema(capsys, monkeypatch)
    # assert
    for name, item in schema.items():
        assert item["type"] in ("string", "integer", "boolean", "array", "object"), name
        assert item["description"] and item["description"].isascii() and "\n" not in item["description"], name
        if name != "title":
            assert item["default"] == getattr(default, name), name


def test_config_schema__harness_and_review_enums__match_validators(tmp_home, capsys, monkeypatch):
    # arrange
    from agent_board.config import FIELD_CHECKS

    schema = _schema(capsys, monkeypatch)
    harness_types = schema["harness"]["shape"]["type"]["enum"]
    review_tools = schema["review"]["shape"]["tool"]["enum"]
    # act / assert
    for kind in harness_types:
        assert not _rejected(FIELD_CHECKS["harness"], _config_with(harness={"type": kind}))
    assert _rejected(FIELD_CHECKS["harness"], _config_with(harness={"type": "other"}))
    for tool in review_tools:
        assert not _rejected(FIELD_CHECKS["review"], _config_with(review={"tool": tool}))
    assert _rejected(FIELD_CHECKS["review"], _config_with(review={"tool": "other"}))


def test_config_schema__tracker_shapes__match_validator(tmp_home, capsys, monkeypatch):
    # arrange
    from agent_board.tracker import validate_config

    tracker = _schema(capsys, monkeypatch)["tracker"]["shape"]

    def ok(cfg):
        return not _rejected(validate_config, cfg)

    # act / assert
    assert set(tracker["types"]) == set(tracker["variants"])
    assert not ok({"type": "other"})
    for kind, variant in tracker["variants"].items():
        example = variant["example"]
        assert example["type"] == kind and ok(example), kind
        for key in variant["required"]:
            assert not ok({k: v for k, v in example.items() if k != key}), (kind, key)
        if kind == "none":
            continue
        assert not ok(dict(example, unknown_key=1)), kind
        for key in variant["optional"]:
            assert key not in variant["required"], (kind, key)
        for auth_type in variant["auth_types"]:
            auth = dict(tracker["auth"][auth_type]["example"])
            assert ok(dict(example, auth=auth)), (kind, auth_type)
            assert not ok(dict(example, auth=dict(auth, token="secret"))), (kind, auth_type)
    for target in tracker["statuses"]["targets"]:
        assert ok(dict(tracker["variants"]["github"]["example"], statuses={"Open": target}))
    assert not ok(dict(tracker["variants"]["github"]["example"], statuses={"Open": "approved"}))


def test_config_schema__tracker_optional_keys__are_all_accepted_keys(tmp_home, capsys, monkeypatch):
    # arrange
    from agent_board.tracker import GITHUB_KEYS, JIRA_KEYS

    variants = _schema(capsys, monkeypatch)["tracker"]["shape"]["variants"]
    # assert
    for kind, keys in (("jira", JIRA_KEYS), ("github", GITHUB_KEYS)):
        assert set(variants[kind]["required"]) | set(variants[kind]["optional"]) | {"type"} == keys, kind


def test_config_schema__ship_placeholders__match_template_validator(tmp_home, capsys, monkeypatch):
    # arrange
    from agent_board.ship import validate_template

    ship = _schema(capsys, monkeypatch)["ship"]["shape"]
    # act / assert
    validate_template(" ".join("{%s}" % p for p in ship["placeholders"]))
    with pytest.raises(ValueError):
        validate_template("push {unknown}")
    assert ship["rules"] and all(rule.isascii() for rule in ship["rules"])


def test_config_schema__human_output__table_row_per_field(tmp_home, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_PROJECT", raising=False)
    from dataclasses import fields

    from agent_board.config import NOT_SETTABLE, ProjectConfig

    # act
    code, out, _ = run(capsys, "config", "schema")
    # assert
    names = [line.split()[0] for line in out.splitlines()[1:] if line.strip() and not line.startswith("-")]
    assert code == 0 and set(names) >= {f.name for f in fields(ProjectConfig)} - set(NOT_SETTABLE)


def test_config_set__decomposition_rules__saved_and_length_capped(srv, capsys):
    # act
    saved = run_json(capsys, "config", "set", "--json", json.dumps({"decomposition_rules": "Одна задача — один репо"}))
    code, _, err = run(capsys, "config", "set", "--json", json.dumps({"decomposition_rules": "x" * 4001}))
    # assert
    assert saved["config"]["decomposition_rules"] == "Одна задача — один репо"
    assert code == 1 and "decomposition_rules" in err
    assert run_json(capsys, "config", "get", "--json")["decomposition_rules"] == "Одна задача — один репо"


@pytest.mark.parametrize("name", ["repos", "resources", "links"])
def test_config_schema__list_item_shapes__match_validators(tmp_home, capsys, monkeypatch, name):
    # arrange
    from agent_board.config import _repos_structure, _validate_links, _validate_resources

    shape = _schema(capsys, monkeypatch)[name]["shape"]
    check = {"repos": _repos_structure, "resources": _validate_resources, "links": _validate_links}[name]
    example = shape["example"]
    # act / assert
    assert set(example) <= set(shape["item"]) and set(shape["required"]) <= set(shape["item"])
    assert not _rejected(check, [example])
    for key in shape["required"]:
        assert _rejected(check, [{k: v for k, v in example.items() if k != key}]), key


def test_config_schema__project_brief__does_not_claim_decomposition_rules(tmp_home, capsys, monkeypatch):
    # act
    schema = _schema(capsys, monkeypatch)
    # assert
    assert "decomposition" not in schema["project_brief"]["description"].lower()


def test_deps__existing_task__replaces_dependencies(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "a"}')
    run_json(capsys, "add-task", "--json", '{"key": "T-2", "title": "b"}')
    run_json(capsys, "add-task", "--json", '{"key": "T-3", "title": "c", "deps": ["T-1"]}')
    # act
    run_json(capsys, "deps", "T-3", "--json", '["T-2"]')
    # assert
    assert run_json(capsys, "show", "T-3")["deps"] == ["T-2"]


def test_deps__cycle__rejected_with_exit_code_1(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "a"}')
    run_json(capsys, "add-task", "--json", '{"key": "T-2", "title": "b", "deps": ["T-1"]}')
    # act
    code, _, err = run(capsys, "deps", "T-1", "--json", '["T-2"]')
    # assert
    assert code == 1 and "цикл" in err
    assert run_json(capsys, "show", "T-1")["deps"] == []


def test_update__task_text_by_user__changes_title_description_done_when_estimate(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    # act
    run_json(capsys, "update", "T-1", "--title", "Новый", "--description", "что делать", "--done-when", "тесты",
             "--estimate", "1.5")
    # assert
    task = run_json(capsys, "show", "T-1")
    assert (task["title"], task["description"], task["done_when"], task["estimate"]) == \
        ("Новый", "что делать", "тесты", 1.5)


@pytest.mark.parametrize("actor", ["agent", "", "project_agent"])
@pytest.mark.parametrize("option,value", [("--title", "t"), ("--description", "d"), ("--done-when", "ничего"),
                                          ("--estimate", "9")])
def test_update__task_text_by_agent__rejected(srv, capsys, monkeypatch, actor, option, value):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor)
    # act
    code, _, err = run(capsys, "update", "T-1", option, value)
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    task = run_json(capsys, "show", "T-1")
    assert (task["title"], task["description"], task["done_when"], task["estimate"]) == ("x", "", "", None)


def test_update__no_actor_env__acts_as_user(srv, capsys):
    # arrange: человек в терминале — пользователь, агентам раннер всегда задаёт AGENT_BOARD_ACTOR
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    # act
    run_json(capsys, "update", "T-1", "--done-when", "тесты", "--rule-violations-json", '[{"rule": "r", "text": "t"}]')
    # assert
    task = run_json(capsys, "show", "T-1")
    assert task["done_when"] == "тесты" and task["rule_violations"] == [{"rule": "r", "text": "t"}]


def test_update__tracker_link_by_user__is_saved(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    # act
    run_json(capsys, "update", "T-1", "--tracker-key", "PRJ-1", "--tracker-url", "https://tracker.example/PRJ-1")
    # assert
    task = run_json(capsys, "show", "T-1")
    assert (task["tracker_key"], task["tracker_url"]) == ("PRJ-1", "https://tracker.example/PRJ-1")


def test_update__empty_tracker_link_by_user__clears_link(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    run_json(capsys, "update", "T-1", "--tracker-key", "PRJ-1", "--tracker-url", "https://tracker.example/PRJ-1")
    # act
    run_json(capsys, "update", "T-1", "--tracker-key", "", "--tracker-url", "")
    # assert
    task = run_json(capsys, "show", "T-1")
    assert (task["tracker_key"], task["tracker_url"]) == (None, None)


@pytest.mark.parametrize("actor", ["agent", "project_agent"])
def test_update__empty_tracker_link_by_agent__rejected(srv, capsys, monkeypatch, actor):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    run_json(capsys, "update", "T-1", "--tracker-key", "PRJ-1")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor)
    # act
    code, _, err = run(capsys, "update", "T-1", "--tracker-key", "")
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "show", "T-1")["tracker_key"] == "PRJ-1"


@pytest.mark.parametrize("actor", ["agent", "", "project_agent"])
def test_update__tracker_link_by_agent__rejected(srv, capsys, monkeypatch, actor):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor)
    # act
    code, _, err = run(capsys, "update", "T-1", "--tracker-key", "PRJ-1")
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "show", "T-1")["tracker_key"] is None


def test_update__rule_violations_by_task_agent__rejected(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "x"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    # act
    code, _, err = run(capsys, "update", "T-1", "--rule-violations-json", "[]")
    # assert
    assert code == 1 and err.strip()


@pytest.mark.parametrize("actor", ["agent", "project_agent"])
def test_deps__by_agent__rejected(srv, capsys, monkeypatch, actor):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "a"}')
    run_json(capsys, "add-task", "--json", '{"key": "T-2", "title": "b"}')
    monkeypatch.setenv("AGENT_BOARD_ACTOR", actor)
    # act
    code, _, err = run(capsys, "deps", "T-2", "--json", '["T-1"]')
    # assert
    assert code == 1 and err.strip()
    monkeypatch.delenv("AGENT_BOARD_ACTOR")
    assert run_json(capsys, "show", "T-2")["deps"] == []


def test_retry_sends_user_actor_and_reports_rejection(srv, capsys):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    run_json(capsys, "stage", "work")
    srv.board_for("pay").update_task("T-1", "system", {"agent_state": "error", "turn_error": "boom"})
    # act
    result = run_json(capsys, "retry", "T-1")
    srv.board_for("pay").update_task("T-1", "system", {"agent_state": "idle"})
    code, _, err = run(capsys, "retry", "T-1")
    # assert
    assert result["events"][0]["data"] == {"key": "T-1", "reason": "retry", "error": "boom"}
    assert code == 1 and "Повторить можно только упавший ход" in err


def test_retry_from_agent_is_rejected(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    # act
    code, _, err = run(capsys, "retry", "_project")
    # assert
    assert code == 1 and "только пользователь" in err


def test_rebase_sends_actor_from_env_and_rejects_task_agent(srv, capsys, monkeypatch):
    # arrange
    run_json(capsys, "add-task", "--json", '{"key": "T-1", "title": "Первая"}')
    run_json(capsys, "stage", "work")
    srv.board_for("pay").update_task("T-1", "system", {"session_id": "s-1", "worktree": "/tmp/wt"})
    # act
    by_user = run_json(capsys, "rebase", "T-1")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "project_agent")
    by_project_agent = run_json(capsys, "rebase", "T-1")
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")
    code, _, err = run(capsys, "rebase", "T-1")
    # assert
    assert by_user["events"][0]["data"] == {"key": "T-1", "reason": "rebase", "by": "user"}
    assert by_project_agent["events"][0]["data"]["by"] == "project_agent"
    assert code == 1 and "агент проекта" in err
