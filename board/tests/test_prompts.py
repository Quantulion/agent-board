from __future__ import annotations

from pathlib import Path

from agent_board.config import ProjectConfig
from agent_board.model import Event, Task
from agent_board.prompts import digest, project_prompt, task_prompt, task_system, wake_message

CLI = "agent-board"
RULES = "Run tests via `make t {x}`; price is $5; never touch /etc."


def _config(**kw):
    kw.setdefault("agent_rules", RULES)
    kw.setdefault("resources", [{"name": "functests", "description": "shared functional test DB"}])
    return ProjectConfig(slug="pay", title="Платежи", language="ru", **kw)


def _task(**kw):
    kw.setdefault("title", "Добавить retry")
    return Task(key="PAY-7", description="Сделать повтор платежа", done_when="Тесты зелёные", repo="svc", **kw)


def _prompts(task=None):
    task = task or _task(deps=["PAY-3"])
    wt = Path("/repo/.agent-board-worktrees/PAY-7")
    return task_system(_config(), CLI) + "\n" + task_prompt(task, _config(), wt, "main", CLI)


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
    assert "Nobody pushes" in text and "only read-only git commands" in text and "git commit" in text
    for cmd in ("status <KEY> in_progress", "status <KEY> in_review", "update <KEY> --summary",
                "--todo-json", "--changed-files-json", "ask <KEY>", "--context", "--recommended",
                "say _project", "lock-run <resource> -- <command>"):
        assert "agent-board " + cmd in text or cmd in text
    assert "agent-board lock-run" in text and "Общайся с пользователем на языке: ru" in text
    assert "$board_cli" not in text and "$language" not in text


def test_task_prompt__new_task_needed__goes_to_project_agent_not_add_task():
    # arrange / act
    text = _prompts()

    # assert
    assert "add-task" not in text
    assert "new task" in text and "say _project" in text.split("new task", 1)[1][:200]


def test_board_cli_is_embedded_not_hardcoded():
    # arrange
    task = _task()

    # act
    text = task_system(_config(), "/opt/ab") + task_prompt(task, _config(), Path("/w"), "main", "/opt/ab")

    # assert
    assert "/opt/ab status PAY-7 in_progress" in text and "/opt/ab lock-run" in text
    assert "`agent-board" not in text


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
    assert "Do not run any git" in text and "Board changes" not in text


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


def test_wake_message_rebase_variants_follow_board_side_rebase():
    # arrange
    base = {"reason": "rebase", "from": "PAY-3", "base": "main"}

    # act
    ok = wake_message([dict(base, rebase_ok=True, conflict_files=[])], "")
    marked = wake_message([dict(base, rebase_ok=True, conflict_files=["a.py", "b.py"])], "")
    failed = wake_message([dict(base, rebase_ok=False, conflict_files=[])], "")

    # assert
    assert "PAY-3" in ok and "main" in ok and "Continue" in ok and "marker" not in ok
    assert "a.py, b.py" in marked and "conflict markers" in marked and "do not commit" in marked
    assert "could not move" in failed and "Do not run any git" in failed and "ask a question" in failed
    for t in (ok, marked, failed):
        assert "git rebase" not in t


def test_wake_message_requested_rebase_names_who_asked_instead_of_dependency():
    # arrange
    base = {"reason": "rebase", "base": "main", "rebase_ok": True, "conflict_files": []}

    # act
    by_user = wake_message([dict(base, by="user")], "")
    by_project_agent = wake_message([dict(base, by="project_agent", conflict_files=["a.py"])], "")

    # assert
    assert "The user asked to bring your branch up to date" in by_user and "moved your work onto main" in by_user
    assert "dependency" not in by_user
    assert "The project agent asked" in by_project_agent and "a.py" in by_project_agent


def test_wake_message_missing_fields_point_to_the_board_instead_of_empty_quotes():
    # arrange
    reasons = [{"reason": "message", "key": "PAY-7"}, {"reason": "answer", "key": "PAY-7"},
               {"reason": "escalation", "key": "_project", "from": "PAY-9"}]

    # act
    text = wake_message(reasons, "", "ab")

    # assert
    assert "ab thread PAY-7" in text and "ab show PAY-7" in text and "ab thread _project" in text
    assert "```\n\n```" not in text and "no option chosen" not in text


def test_wake_message_escalation_quotes_text_when_present():
    # arrange / act
    text = wake_message([{"reason": "escalation", "from": "PAY-9", "text": "which DB?"}], "")

    # assert
    assert "PAY-9" in text and "which DB?" in text


def test_wake_message__user_comment_on_task__should_name_the_task_and_quote_the_comment():
    # arrange
    reasons = [{"reason": "message", "key": "_project", "task": "PAY-7", "message_id": "m1", "text": "раздели `T4`"}]

    # act
    text = wake_message(reasons, "", "ab")

    # assert
    assert "Comment from the user on task PAY-7" in text and "раздели `T4`" in text and "m1" in text
    assert "ab say PAY-7" in text


def test_wake_message__user_comment_on_task_without_text__should_point_to_the_task_thread():
    # arrange
    reasons = [{"reason": "message", "key": "_project", "task": "PAY-7"}]

    # act
    text = wake_message(reasons, "", "ab")

    # assert
    assert "PAY-7" in text and "ab thread PAY-7" in text and "ab thread _project" not in text


def test_wake_message_simple_reasons_render_without_fields():
    # arrange
    reasons = [{"reason": r} for r in ("interrupted", "unblocked", "ready", "manual", "proposal_rejected")]

    # act
    text = wake_message(reasons, "")

    # assert
    assert text.count("\n\n") == 4 and "interrupted" in text and "rejected" in text and "unblocked" in text


def test_task_system_conflict_marker_and_git_rules():
    # arrange / act
    text = task_system(_config(), CLI)

    # assert
    assert "<<<<<<<" in text and ">>>>>>>" in text and "until no marker lines remain" in text
    for banned in ("git merge --abort", "git rebase --continue", "git add", "git stash"):
        assert banned in text and text.index("Never run", text.index("<<<<<<<")) < text.index(banned)
    assert "unless the board asks" not in text
    assert "only read-only git commands" in text


def test_task_system_resources_section_only_when_resources_exist():
    # arrange
    with_res = task_system(_config(), CLI)
    without = task_system(_config(resources=[]), CLI)

    # assert
    assert "functests — shared functional test DB" in with_res and "lock-run" in with_res
    assert "lock-run" not in without and "Shared resources" not in without


def test_task_system_statuses_end_of_turn_and_research_rules():
    # arrange / act
    text = task_system(_config(), CLI)

    # assert
    assert "Set only in_progress and in_review" in text and "never set other statuses" in text
    assert "approved only when" not in text
    assert "Before ending any turn" in text and "reports/" in text and "long tool timeout" in text


def test_project_prompt_escalation_answered_to_task_thread():
    # arrange / act
    text = project_prompt(_config(), {"tasks": []}, CLI)

    # assert
    assert "escalation" in text and "say <KEY>" in text and "only for the user" in text


def test_wake_message_missing_from_and_id_point_to_the_board_without_placeholders_like_question_mark():
    # arrange
    reasons = [{"reason": "returned", "key": "PAY-7"}, {"reason": "rebase", "key": "PAY-7", "base": "main", "rebase_ok": True},
               {"reason": "proposal_rejected", "key": "_project"}, {"reason": "escalation", "key": "_project"}]

    # act
    text = wake_message(reasons, "", "ab")

    # assert
    assert "a task agent" not in text and "?" not in text
    assert "ab show PAY-7" in text and "ab thread _project" in text
    assert "ab events --since" in text and "ab say <KEY> --text" in text


def test_wake_message_rebase_with_several_dependency_branches_asks_to_merge():
    # arrange
    reason = {"reason": "rebase", "from": "PAY-3", "base": "main", "rebase_ok": False, "merge_branches": ["PAY-3", "PAY-4"]}
    # act
    text = wake_message([reason], "")
    # assert
    assert "PAY-3, PAY-4" in text and "merge" in text and "Do not run any git" in text
    assert "could not move" not in text


def test_project_prompt_explains_recording_rule_violations():
    # act
    text = project_prompt(_config(), {"tasks": [], "proposals": []}, CLI)
    # assert
    assert "update <KEY> --rule-violations-json" in text


def test_wake_message__review_round__should_quote_each_comment_and_give_reply_command():
    # arrange
    reason = {"key": "PAY-7", "reason": "review", "round": 2, "binary": "crit", "crit_dir": "/b/pay/crit",
              "comments": [{"id": "c_1", "path": "src/a.py", "line": 12, "body": "Вынеси в `helper`"},
                           {"id": "r_2", "path": None, "line": None, "body": "В целом ок"}]}
    # act
    text = wake_message([reason], "", CLI)
    # assert
    assert "round 2" in text and "2 unresolved comments" in text
    assert "c_1" in text and "src/a.py:12" in text and "Вынеси в `helper`" in text
    assert "r_2" in text and "В целом ок" in text
    assert "crit comment --reply-to <id> -o /b/pay/crit --author agent" in text
    assert "Do not run crit" in text and "--resolve" in text
    assert "agent-board status PAY-7 in_review" in text and "end your turn" in text


def test_wake_message__review_round_with_custom_binary_and_spaces__should_quote_paths():
    # arrange
    reason = {"key": "PAY-7", "reason": "review", "round": 1, "binary": "/opt/my crit", "crit_dir": "/b/my pay/crit",
              "comments": [{"id": "c_1", "path": "a.py", "line": None, "body": "x"}]}
    # act
    text = wake_message([reason], "", CLI)
    # assert
    assert "'/opt/my crit' comment --reply-to <id> -o '/b/my pay/crit'" in text
    assert "a.py" in text and "a.py:None" not in text


def test_wake_message__review_without_comments__should_point_to_crit_comments():
    # arrange
    reason = {"key": "PAY-7", "reason": "review", "round": 1}
    # act
    text = wake_message([reason], "", CLI)
    # assert
    assert "crit comments" in text and "None" not in text


def test_wake_message__returned_with_comment__should_quote_it():
    # arrange
    reason = {"key": "PAY-7", "reason": "returned", "from": "in_review", "message_id": "m1", "text": "Поправь тесты"}
    # act
    text = wake_message([reason], "", CLI)
    # assert
    assert "from status `in_review`" in text and "Поправь тесты" in text and "```" in text


def test_task_system_explains_review_rounds():
    # arrange / act
    text = task_system(_config(), CLI)
    # assert
    assert "## Review" in text and "crit comment --reply-to" in text
    assert "Never run crit" in text and "--resolve" in text


def test_project_prompt__decomposition_rules__included_or_none():
    # act
    with_rules = project_prompt(_config(decomposition_rules="Одна задача — один репозиторий"), {"tasks": []}, CLI)
    without = project_prompt(_config(), {"tasks": []}, CLI)
    # assert
    assert "## Decomposition rules\nОдна задача — один репозиторий" in with_rules
    assert "## Decomposition rules\n(none)" in without
