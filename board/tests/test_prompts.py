from __future__ import annotations

from pathlib import Path

from agent_board.config import ProjectConfig
from agent_board.model import Event, Task
from agent_board.prompts import digest, project_prompt, task_prompt, task_system, wake_message

CLI = "agent-board"
RULES = "Run tests via `make t {x}`; price is $5; never touch /etc."


def _config(**kw):
    kw.setdefault("agent_rules", RULES)
    kw.setdefault("resources", [{"name": "functests"}])
    return ProjectConfig(slug="pay", title="Платежи", language="ru", **kw)


def _task(**kw):
    kw.setdefault("title", "Добавить retry")
    return Task(key="PAY-7", description="Сделать повтор платежа", done_when="Тесты зелёные", repo="svc", **kw)


def _prompts(task=None):
    task = task or _task(deps=["PAY-3"])
    wt = Path("/repo/.agent-board-worktrees/PAY-7")
    return task_system(_config()) + "\n" + task_prompt(task, _config(), wt, "main", CLI)


def test_task_prompt_contains_task_specifics_and_rules():
    # arrange / act
    text = _prompts()

    # assert
    for expected in ("PAY-7", "Добавить retry", "Тесты зелёные", "Сделать повтор платежа",
                     "/repo/.agent-board-worktrees/PAY-7", "main", "PAY-3", RULES):
        assert expected in text


def test_task_prompt_forbids_commit_and_push_and_describes_cli():
    # arrange / act
    text = _prompts()

    # assert
    assert "git commit" in text and "git push" in text and "Never run" in text
    for cmd in ("status <KEY> in_progress", "status <KEY> in_review", "update <KEY> --summary",
                "--todo-json", "--changed-files-json", "ask <KEY>", "--context", "--recommended",
                "say _project", "lock-run <resource> -- <command>"):
        assert "agent-board " + cmd in text or cmd in text
    assert "agent-board lock-run" in text and "Общайся с пользователем на языке: ru" in text
    assert "$board_cli" not in text and "$language" not in text


def test_board_cli_is_embedded_not_hardcoded():
    # arrange
    task = _task()

    # act
    text = task_system(_config(), "/opt/ab") + task_prompt(task, _config(), Path("/w"), "main", "/opt/ab")

    # assert
    assert "/opt/ab status PAY-7 in_progress" in text and "agent-board " not in text.replace("agent-board, a local", "")


def test_special_characters_in_task_fields_do_not_break_rendering():
    # arrange
    task = Task(key="K-1", title="fix {} and $x and ${y} {title}", description="costs $5 {0}", done_when="$done")

    # act
    text = task_prompt(task, _config(agent_rules="a {b} $c"), Path("/w"), "main", CLI)

    # assert
    assert "fix {} and $x and ${y} {title}" in text and "costs $5 {0}" in text and "a {b} $c" in text


def test_project_prompt_contains_brief_snapshot_and_commands():
    # arrange
    config = _config(project_brief="Платёжный сервис, не ломать API")
    snapshot = {"tasks": [{"key": "PAY-1", "title": "A", "display_status": "blocked", "open_deps": ["PAY-0"]}],
                "proposals": [{"id": "p1", "kind": "add_task", "status": "open"}]}

    # act
    text = project_prompt(config, snapshot, CLI)

    # assert
    assert "Платёжный сервис, не ломать API" in text and "PAY-1 [blocked] A" in text and "p1" in text
    for cmd in ("state", "show <KEY>", "thread", "events --since", "say _project", "say <KEY>", "propose add_task"):
        assert cmd in text
    assert RULES in text


def test_wake_message_merges_reasons_in_order_and_quotes_user_message_verbatim():
    # arrange
    reasons = [
        {"reason": "answer", "question": "Какой retry?", "choice": "Экспоненциальный", "comment": "и лог"},
        {"reason": "message", "message_id": "m42", "text": "Привет {x}\n```код``` $1"},
        {"reason": "rebase", "from": "PAY-3", "base": "PAY-3", "rebase_ok": False},
    ]

    # act
    text = wake_message(reasons, "")

    # assert
    assert "Привет {x}\n```код``` $1" in text and "m42" in text
    assert text.index("Экспоненциальный") < text.index("m42") < text.index("PAY-3")
    assert "conflicts" in text and "Board changes" not in text


def test_wake_message_covers_returned_interrupted_and_escalation_and_appends_digest():
    # arrange
    reasons = [{"reason": "returned", "from": "in_review"}, {"reason": "interrupted"},
               {"reason": "escalation", "from": "PAY-9"}]

    # act
    text = wake_message(reasons, "#5 PAY-9: in_progress -> in_review (by agent)")

    # assert
    assert "in_review" in text and "interrupted" in text and "PAY-9" in text
    assert text.rstrip().endswith("#5 PAY-9: in_progress -> in_review (by agent)")
    assert "Board changes" in text


def test_digest_truncates_to_limit_and_mentions_turn_failure():
    # arrange
    events = [Event(seq=i, type="task_added", at="", data={"key": "T%d" % i}) for i in range(1, 11)]
    events.append(Event(seq=11, type="turn_failed", at="", data={"key": "T3", "error": "boom"}))
    events.append(Event(seq=12, type="thread_read", at="", data={"thread": "T3"}))

    # act
    text = digest(events, limit=3)

    # assert
    lines = text.splitlines()
    assert lines[0] == "…ещё 8 событий" and len(lines) == 4
    assert "FAILED" in lines[-1] and "boom" in lines[-1] and "T10" in lines[-2]


def test_prompt_templates_are_listed_in_package_data():
    # arrange
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")

    # act / assert
    assert "prompts/*.md" in pyproject
