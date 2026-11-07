from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from test_setup_reference import _FENCE, _INLINE, _cli, _subcommand

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "agent-board"
SKILL = SKILL_DIR / "SKILL.md"
FILES = [SKILL] + sorted((SKILL_DIR / "references").glob("*.md"))
MAX_SKILL_LINES = 200
MAX_DESCRIPTION = 1024
_REFERENCED = re.compile(r"(?<![\w/])((?:references|scripts)/[A-Za-z0-9_.-]+)")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def _frontmatter() -> dict:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md должен начинаться с frontmatter"
    head = text[4:text.index("\n---\n", 4)]
    fields = {}
    for line in head.splitlines():
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    return fields


def _spans(path: Path):
    """Код из блоков ``` (построчно) и из `…` вне блоков."""
    text = path.read_text(encoding="utf-8")
    spans = [line.strip() for block in _FENCE.findall(text) for line in block.splitlines() if line.strip()]
    spans += _INLINE.findall(_FENCE.sub("", text))
    return spans


def _commands():
    """Команды доски из всех файлов скилла; префикс переменных окружения (`AGENT_BOARD_ACTOR=user`) отбрасывается."""
    result = []
    for path in FILES:
        for span in _spans(path):
            try:
                tokens = shlex.split(span)
            except ValueError:
                continue
            while tokens and _ENV.match(tokens[0]):
                tokens = tokens[1:]
            if len(tokens) > 1 and tokens[0] == "agent-board":
                result.append((path.name, tokens))
    return result


def _option(tokens, name):
    return tokens[tokens.index(name) + 1]


def test_skill__frontmatter__name_and_trigger_description():
    # act
    fields = _frontmatter()
    # assert
    assert fields["name"] == "agent-board"
    assert fields["description"].startswith("Use when")
    assert len(fields["description"]) <= MAX_DESCRIPTION


def test_skill__length__within_limit():
    # act
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    # assert
    assert len(lines) <= MAX_SKILL_LINES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_skill__referenced_files__exist(path):
    # arrange
    referenced = set(_REFERENCED.findall(path.read_text(encoding="utf-8")))
    # act
    missing = sorted(r for r in referenced if not (SKILL_DIR / r).exists())
    # assert
    assert not missing, missing


def test_skill__references__all_linked_from_skill_md():
    # arrange
    text = SKILL.read_text(encoding="utf-8")
    # act
    unlinked = [p.name for p in FILES[1:] if "references/" + p.name not in text]
    # assert
    assert not unlinked, unlinked


@pytest.mark.slow
def test_skill__commands__exist_with_their_options(tmp_path):
    # arrange
    commands = _commands()
    checked = {}
    # act / assert
    assert {name for name, _ in commands} >= {"SKILL.md", "intake.md", "decomposition-rules.md", "resume.md"}
    for name, tokens in commands:
        words, flags = _subcommand(tokens)
        assert words, (name, tokens)
        key = tuple(words)
        if key not in checked:
            res = _cli(words + ["--help"], tmp_path)
            assert res.returncode == 0, (name, tokens, res.stderr)
            checked[key] = res.stdout
        for flag in flags:
            if flag != "--project":
                assert flag in checked[key], (name, tokens, flag)


def test_skill__add_task_examples__use_creatable_fields():
    # arrange
    from agent_board.board import CREATABLE_FIELDS, TASK_KINDS

    tasks = [json.loads(_option(t, "--json")) for _, t in _commands() if "add-task" in t]
    # act / assert
    assert tasks
    for task in tasks:
        assert set(task) <= set(CREATABLE_FIELDS), task
        assert task["kind"] in TASK_KINDS and all(isinstance(d, str) for d in task["deps"])


def test_skill__rule_violation_and_deps_examples__have_board_shape():
    # arrange
    commands = [t for _, t in _commands()]
    violations = [json.loads(_option(t, "--rule-violations-json")) for t in commands if "--rule-violations-json" in t]
    deps = [json.loads(_option(t, "--json")) for t in commands if "deps" in t]
    # act / assert
    assert violations and deps
    for items in violations:
        assert all(set(item) == {"rule", "text"} for item in items), items
    for keys in deps:
        assert isinstance(keys, list) and all(isinstance(k, str) for k in keys), keys


def test_skill__commands__no_actor_override():
    # arrange
    spans = [(path.name, span) for path in FILES for span in _spans(path)]
    # act: команды пользователя идут от него без переменных; переменная превращает сессию в агента
    overridden = [(name, span) for name, span in spans if re.match(r"AGENT_BOARD_[A-Z_]+=", span)]
    # assert
    assert not overridden, overridden


def _signature(tokens):
    """Подкоманда без флагов: слова как есть, позиционные аргументы и плейсхолдеры — `<arg>`."""
    args = tokens[1:]
    if args[:1] == ["--project"]:
        args = args[2:]
    result = []
    for token in args[:3]:
        if token.startswith("--") or token == "…":
            break
        result.append(token if re.fullmatch(r"[a-z][a-z-]*", token) else "<arg>")
    return tuple(result)


def _forbidden():
    text = SKILL.read_text(encoding="utf-8")
    table = text[text.index("## Never"):]
    table = table[:table.index("\n## ", 1)]
    rows = [line.split("|")[1] for line in table.splitlines() if line.startswith("| ")]
    return {_signature(shlex.split(span)) for row in rows for span in _INLINE.findall(row)
            if span.startswith("agent-board ")}


def test_skill__forbidden_commands__not_used_anywhere():
    # arrange
    forbidden = _forbidden()
    never = SKILL.read_text(encoding="utf-8").split("## Never")[1]
    # act
    used = [(name, tokens) for name, tokens in _commands()
            if not (name == "SKILL.md" and " ".join(tokens) in never)]
    clashes = [(name, tokens) for name, tokens in used
               if any(_signature(tokens)[:len(f)] == f for f in forbidden)]
    # assert
    assert {("stage",), ("tracker", "create"), ("tracker", "sync"), ("ship",)} <= forbidden, forbidden
    assert not clashes, clashes


def test_skill__config_set_examples__use_schema_fields(tmp_path):
    # arrange
    res = _cli(["config", "schema", "--json"], tmp_path)
    fields = {f["name"] for f in json.loads(res.stdout)["fields"]}
    payloads = [json.loads(_option(t, "--json")) for name, t in _commands() if name != "setup.md" and "set" in t]
    # act / assert
    assert payloads
    for payload in payloads:
        assert set(payload) <= fields, payload
