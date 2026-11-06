from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "skills" / "agent-board" / "scripts" / "agent-board"
SETUP = ROOT / "skills" / "agent-board" / "references" / "setup.md"
MAX_LINES = 150
_FENCE = re.compile(r"^```.*?$(.*?)^```", re.S | re.M)
_INLINE = re.compile(r"`([^`\n]+)`")
_WORD = re.compile(r"^[a-z][a-z-]*$")
_SNAKE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
_FIELD_HEADING = re.compile(r"^###.*→ `([a-z_]+)`", re.M)


def _text() -> str:
    return SETUP.read_text(encoding="utf-8")


def _spans():
    """Код из блоков ``` (построчно) и из `…` вне блоков."""
    text = _text()
    spans = [line.strip() for block in _FENCE.findall(text) for line in block.splitlines() if line.strip()]
    spans += _INLINE.findall(_FENCE.sub("", text))
    return spans


def _commands():
    return [shlex.split(span) for span in _spans() if span.startswith("agent-board ")]


def _cli(args, tmp_path):
    env = dict(os.environ, AGENT_BOARD_HOME=str(tmp_path), AGENT_BOARD_PYTHON=sys.executable)
    env.pop("AGENT_BOARD_PROJECT", None)
    return subprocess.run([str(WRAPPER)] + list(args), capture_output=True, text=True, timeout=60, env=env)


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    res = _cli(["config", "schema", "--json"], tmp_path_factory.mktemp("home"))
    assert res.returncode == 0, res.stderr
    return {f["name"]: f for f in json.loads(res.stdout)["fields"]}


def _names(value) -> set:
    """Все имена ключей и строковые значения схемы: поля, ключи вложенных объектов, перечисления."""
    if isinstance(value, dict):
        return set(value) | set().union(*(_names(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_names(v) for v in value)) if value else set()
    return {value} if isinstance(value, str) else set()


def _subcommand(tokens):
    args = tokens[1:]
    if args[:1] == ["--project"]:
        args = args[2:]
    words = []
    for token in args:
        if not _WORD.match(token) or len(words) == 2:
            break
        words.append(token)
    return words, [t for t in args if t.startswith("--")]


def test_setup__length__within_limit():
    # act
    lines = _text().splitlines()
    # assert
    assert len(lines) <= MAX_LINES


def test_setup__commands__exist_with_their_options(tmp_path):
    # arrange
    commands = _commands()
    checked = set()
    # act / assert
    assert commands
    if any("--project" in tokens for tokens in commands):
        res = _cli(["--help"], tmp_path)
        assert res.returncode == 0 and "--project" in res.stdout
    for tokens in commands:
        words, flags = _subcommand(tokens)
        assert words, tokens
        if tuple(words) in checked:
            continue
        checked.add(tuple(words))
        res = _cli(words + ["--help"], tmp_path)
        assert res.returncode == 0, (tokens, res.stderr)
        for flag in flags:
            if flag != "--project":
                assert flag in res.stdout, (tokens, flag)


def test_setup__config_set_examples__use_schema_fields_and_shapes(schema):
    # arrange
    tracker = schema["tracker"]["shape"]
    payloads = [json.loads(t[t.index("--json") + 1]) for t in _commands() if "set" in t and "--json" in t]
    # act / assert
    assert payloads
    for fields in payloads:
        assert set(fields) <= set(schema), fields
        for name, value in fields.items():
            shape = schema[name].get("shape") or {}
            if name == "tracker":
                variant = tracker["variants"][value["type"]]
                assert set(value) <= set(variant["required"]) | set(variant["optional"]) | {"type"}, value
                assert set(value["auth"]) <= set(tracker["auth"][value["auth"]["type"]]["keys"])
            elif name == "ship":
                assert set(value) <= set(shape["keys"])
            elif name == "harness":
                assert set(value) <= set(shape) - {"note"} and value["type"] in shape["type"]["enum"]
            elif "item" in shape:
                assert all(set(item) <= set(shape["item"]) for item in value), value


def test_setup__inline_json__matches_schema(schema):
    # arrange
    tracker = schema["tracker"]["shape"]
    objects = []
    for span in _INLINE.findall(_FENCE.sub("", _text())):
        try:
            value = json.loads(span)
        except ValueError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    # act / assert
    assert objects
    for value in objects:
        if "tool" in value:
            assert set(value) <= set(schema["review"]["shape"]) and \
                value["tool"] in schema["review"]["shape"]["tool"]["enum"]
        elif "type" in value:
            assert set(value) <= set(tracker["auth"][value["type"]]["keys"]), value
        else:
            # предлагаемый маппинг только двигает задачи вперёд: ранние статусы трекера не возвращают задачу назад
            assert all(v is None or v in tracker["statuses"]["targets"] for v in value.values()), value
            assert not set(value.values()) & {"not_started", "in_progress"}, value


def test_setup__named_fields__exist_in_schema(schema):
    # arrange
    known = _names(list(schema.values()))
    headings = _FIELD_HEADING.findall(_text())
    snake = [s for s in _spans() if _SNAKE.match(s)]
    # act / assert
    assert headings and set(headings) <= set(schema), headings
    assert snake and set(snake) <= known, set(snake) - known


def test_setup__ship_examples__pass_template_validation():
    # arrange
    from agent_board.ship import validate_template

    examples = [s for s in _INLINE.findall(_FENCE.sub("", _text())) if "{branch}" in s and " " in s]
    # act / assert
    assert examples
    for command in examples:
        validate_template(command)


def test_setup__config_set_examples__pass_dry_run(tmp_home, tmp_path, capsys, monkeypatch):
    # arrange
    from agent_board.cli import main
    from agent_board.server import BoardServer

    repo = tmp_path / "api"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)
    monkeypatch.delenv("AGENT_BOARD_PROJECT", raising=False)
    assert main(["init", "pay", "--title", "Платежи"]) == 0
    server = BoardServer(port=0, runner=False, tracker_sync=False)
    server.start()
    examples = [t for t in _commands() if "set" in t and "--dry-run" in t]
    try:
        # act / assert
        assert examples
        for tokens in examples:
            args = [a.replace("<slug>", "pay").replace("/abs/path/api", str(repo)) for a in tokens[1:]]
            capsys.readouterr()
            code = main(args)
            out = capsys.readouterr()
            assert code == 0, (tokens, out.err)
            assert json.loads(out.out)["saved"] is False
    finally:
        server.stop()
