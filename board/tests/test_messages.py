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
