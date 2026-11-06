from __future__ import annotations

import ast
import json
import re
import string
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_board import messages
from agent_board.cli import build_parser, main
from agent_board.messages import CATALOG, msg
from agent_board.server import BoardServer

CYRILLIC = re.compile("[А-Яа-яЁё]")
PACKAGE = Path(messages.__file__).resolve().parent


def _fields(template):
    return sorted(name for _, name, _, _ in string.Formatter().parse(template) if name is not None)


@pytest.fixture
def srv(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_PROJECT", "pay")
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)
    monkeypatch.delenv("AGENT_BOARD_TASK", raising=False)
    assert main(["init", "pay", "--title", "Payments"]) == 0
    server = BoardServer(port=0)
    server.start()
    yield server
    server.stop()


def run(capsys, *argv):
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- каталог ---

@pytest.mark.parametrize("key", sorted(CATALOG))
def test_catalog__every_message__has_both_languages_with_same_placeholders(key):
    # arrange
    en, ru = CATALOG[key]
    # act
    en_fields, ru_fields = _fields(en), _fields(ru)
    # assert
    assert en.strip() and ru.strip()
    assert en_fields == ru_fields
    assert not CYRILLIC.search(en), en


def test_msg__no_language_setting__english(monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    # act
    text = msg("board.unknown_task", key="T-1")
    # assert
    assert text == "Unknown task: T-1"
    assert text.render("ru") == "Неизвестная задача: T-1"


def test_msg__russian_setting__russian_and_nested_params_follow_language(monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_LANG", "ru")
    # act
    text = msg("config.not_settable", name="slug", reason=msg("config.slug_fixed"))
    # assert
    assert text == "Поле slug нельзя менять в настройках: имя проекта не меняется"
    assert text.render("en") == "Field slug cannot be changed in settings: the project name does not change"


@pytest.mark.parametrize("value, expected", [
    (None, "en"), ("", "en"), ("en", "en"), ("English", "en"), ("de", "en"),
    ("ru", "ru"), ("RU", "ru"), ("ru_RU.UTF-8", "ru"), ("ru-RU", "ru"), ("русский", "ru"),
])
def test_normalize__language_values__mapped_to_supported(value, expected):
    # act
    result = messages.normalize(value)
    # assert
    assert result == expected


def test_localize__nested_payload__messages_rendered_in_requested_language(monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    payload = {"errors": [{"key": "A", "error": msg("board.unknown_task", key="A")}], "ok": False, "n": 1}
    # act
    result = messages.localize(payload, "ru")
    # assert
    assert result == {"errors": [{"key": "A", "error": "Неизвестная задача: A"}], "ok": False, "n": 1}


@pytest.mark.parametrize("name", ["cli.py", "lifecycle.py", "server.py"])
def test_source__cli_modules__have_no_russian_string_literals(name):
    # arrange
    tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)}
    # act
    russian = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and CYRILLIC.search(node.value) and id(node) not in docstrings]
    # assert
    assert russian == []


# --- системные сообщения доски ---

SYSTEM_MESSAGE_MODULES = ["board.py", "runner.py", "harness.py", "review.py", "ship.py"]


def _russian_literals(name):
    """Русские строковые литералы модуля, кроме докстрингов, описаний событий и промптов агентам."""
    tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
    skipped = {id(node.body[0].value) for node in ast.walk(tree)
               if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
               and node.body and isinstance(node.body[0], ast.Expr)}
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) \
            else []
        if any(isinstance(t, ast.Name) and t.id.endswith(("_PROMPT", "_TYPES", "_REASONS")) for t in targets):
            skipped.update(id(sub) for sub in ast.walk(node.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            skipped.update(id(sub) for sub in ast.walk(node))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and CYRILLIC.search(node.value) and id(node) not in skipped]


@pytest.mark.parametrize("name", SYSTEM_MESSAGE_MODULES)
def test_source__system_message_modules__texts_come_from_catalog(name):
    # act
    russian = _russian_literals(name)
    # assert
    assert russian == []


@pytest.fixture
def make_board(tmp_home):
    from agent_board.board import Board
    from agent_board.config import ProjectConfig
    from agent_board.store import ProjectStore

    def make(language):
        store = ProjectStore("pay")
        store.create(ProjectConfig(slug="pay", title="Payments", language=language))
        return Board(store)
    return make


def _add(board, key, deps=()):
    from agent_board.board import new_task_from_dict
    board.add_task(new_task_from_dict({"key": key, "title": key, "deps": list(deps)}), "user")


@pytest.mark.parametrize("language, expected", [
    ("en", "Applied proposal p1: dependencies of task B: none"),
    ("ru", "Применено предложение p1: зависимости задачи B: нет"),
])
def test_apply_proposal__project_language__system_message_in_that_language(make_board, monkeypatch, language,
                                                                           expected):
    # arrange: язык процесса противоположен языку проекта
    monkeypatch.setenv(messages.LANG_ENV, "ru" if language == "en" else "en")
    board = make_board(language)
    _add(board, "A")
    _add(board, "B", ["A"])
    board.propose("set_deps", {"key": "B", "deps": []}, "")
    # act
    board.apply_proposal("p1")
    # assert
    texts = [m.text for m in board.store.read_thread("B")]
    assert texts == [expected]
    assert type(texts[0]) is str


@pytest.mark.parametrize("language", ["en", "ru"])
def test_board_writes__catalog_messages__stored_in_project_language(make_board, monkeypatch, language):
    # arrange
    monkeypatch.setenv(messages.LANG_ENV, "ru" if language == "en" else "en")
    board = make_board(language)
    _add(board, "A")
    error = msg("run.not_started", error=msg("run.no_worktree"))
    expected = error.render(language)
    # act
    board.update_task("A", "system", {"turn_error": error, "review": {"state": "idle", "error": error}})
    board.record("turn_failed", {"key": "A", "error": error})
    board.update_meta({"project_turn_error": error})
    board.post_message("A", "system", error)
    # assert
    task = board.store.load_tasks()["A"]
    assert task.turn_error == expected and task.review["error"] == expected
    assert [e.data["error"] for e in board.store.read_events() if e.type == "turn_failed"] == [expected]
    assert board.store.load_meta()["project_turn_error"] == expected
    assert board.store.read_thread("A")[-1].text == expected


def test_board_writes__stored_messages__not_rewritten_when_language_changes(make_board):
    # arrange
    board = make_board("ru")
    _add(board, "A")
    board.post_message("A", "system", msg("run.interrupted"))
    # act
    board.update_config({"language": "en"})
    board.post_message("A", "system", msg("run.interrupted"))
    # assert
    assert [m.text for m in board.store.read_thread("A")] == [msg("run.interrupted").render("ru"),
                                                               msg("run.interrupted").render("en")]


# --- CLI ---

def test_cli__no_language_setting__prints_english(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    # act
    local = run(capsys, "show", "NOPE")
    remote = run(capsys, "status", "NOPE", "in_progress")
    usage = run(capsys, "status", "NOPE")
    # assert
    assert local == (1, "", "Unknown task: NOPE\n")
    assert remote == (1, "", "Unknown task: NOPE\n")
    assert usage[0] == 1 and usage[2].startswith("Usage: agent-board status")


def test_cli__russian_setting__prints_russian(srv, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_LANG", "ru")
    # act
    local = run(capsys, "show", "NOPE")
    remote = run(capsys, "status", "NOPE", "in_progress")
    # assert
    assert local == (1, "", "Неизвестная задача: NOPE\n")
    assert remote == (1, "", "Неизвестная задача: NOPE\n")


@pytest.mark.parametrize("lang, expected", [
    (None, "Dependencies form a cycle: A → B → A"),
    ("ru", "Зависимости образуют цикл: A → B → A"),
])
def test_cli__board_error__printed_in_cli_language(srv, capsys, monkeypatch, lang, expected):
    # arrange
    for key, deps in (("A", []), ("B", ["A"])):
        assert run(capsys, "add-task", "--json", json.dumps({"key": key, "title": key, "deps": deps}))[0] == 0
    if lang is None:
        monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    else:
        monkeypatch.setenv("AGENT_BOARD_LANG", lang)
    # act
    code, out, err = run(capsys, "deps", "A", "--json", '["B"]')
    # assert
    assert (code, out, err.strip()) == (1, "", expected)


@pytest.mark.parametrize("lang, expected", [(None, "the board is not running"), ("ru", "доска не запущена")])
def test_cli__no_server__message_in_cli_language(tmp_home, capsys, monkeypatch, lang, expected):
    # arrange
    if lang is None:
        monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    else:
        monkeypatch.setenv("AGENT_BOARD_LANG", lang)
    # act
    stop = run(capsys, "stop")
    status = run(capsys, "status")
    # assert
    assert stop == (0, expected + "\n", "")
    assert status[0] == 0 and status[1].startswith(expected)


@pytest.mark.parametrize("lang, expected", [(None, "start the board"), ("ru", "запустить доску")])
def test_cli__help__in_cli_language(monkeypatch, lang, expected):
    # arrange
    if lang is None:
        monkeypatch.delenv("AGENT_BOARD_LANG", raising=False)
    else:
        monkeypatch.setenv("AGENT_BOARD_LANG", lang)
    # act
    text = build_parser().format_help()
    # assert
    assert expected in text


# --- HTTP API ---

@pytest.mark.parametrize("header, expected", [
    (None, "Unknown task: NOPE"), ("en", "Unknown task: NOPE"), ("ru", "Неизвестная задача: NOPE"),
])
def test_api__error__in_language_of_request(srv, header, expected):
    # arrange
    headers = {"X-Board-Token": srv.token, "Content-Type": "application/json"}
    if header is not None:
        headers["X-Board-Lang"] = header
    req = urllib.request.Request("http://127.0.0.1:%d/api/p/pay/tasks/NOPE/transition" % srv.port,
                                 data=b'{"to": "in_progress", "actor": "agent"}', method="POST", headers=headers)
    # act
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=10)
    # assert
    assert caught.value.code == 404
    assert json.loads(caught.value.read().decode("utf-8")) == {"error": expected}
