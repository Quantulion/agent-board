from __future__ import annotations

import json

import pytest

from agent_board.cli import build_parser, main
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
    assert wakes == [{"key": "_project", "reason": "escalation", "from": "T-1"}]


def test_say_without_task_env_does_not_escalate(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)

    # act
    out = run_json(capsys, "say", "_project", "--text", "просто так")

    # assert
    assert [e["type"] for e in out["events"]] == ["message_posted"]

