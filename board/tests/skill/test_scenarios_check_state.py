from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS = Path(__file__).resolve().parent / "scenarios"
sys.path.insert(0, str(SCENARIOS))

import check_state  # noqa: E402
import make_env  # noqa: E402
from common import cli  # noqa: E402

SLUG = "pay"


@pytest.fixture
def env(tmp_path, capsys):
    env_dir = tmp_path / "env"
    assert make_env.main([str(env_dir)]) == 0
    yield env_dir
    cli(env_dir, ["stop"], check=False)


def _project(env, config=None):
    cli(env, ["init", SLUG, "--title", "Pay"])
    cli(env, ["serve", "--detach"])
    cfg = {"repos": [{"name": "repo", "path": str(env / "repo"), "base": "main"}], "harness": {"type": "fake"},
           "review": {"tool": "none"}}
    cfg.update(config or {})
    cli(env, ["--project", SLUG, "config", "set", "--json", json.dumps(cfg)])


def _task(env, key, title, deps=(), kind="code", done_when="Tests pass", estimate=1):
    task = {"key": key, "title": title, "description": "d", "done_when": done_when, "repo": "repo", "kind": kind,
            "estimate": estimate, "deps": list(deps)}
    cli(env, ["--project", SLUG, "add-task", "--json", json.dumps(task)])


def _plan_board(env, violation=True, brief=True, links=True):
    _project(env)
    _task(env, "PAY-1", "Add storage module")
    _task(env, "PAY-2", "Add CLI commands on top of storage", ["PAY-1"], estimate=3)
    _task(env, "PAY-3", "Add help output and an end-to-end test", ["PAY-2"])
    _task(env, "PAY-4", "Add logging configuration")
    _task(env, "PAY-5", "Update README usage section")
    _task(env, "PAY-6", "Research export formats", kind="research")
    if violation:
        cli(env, ["--project", SLUG, "update", "PAY-2", "--rule-violations-json",
                  json.dumps([{"rule": "Task <= 2 days", "text": "3 days. Fix: split."}])])
    fields = {}
    if brief:
        fields["project_brief"] = "Goal: items CLI."
    if links:
        fields["links"] = [{"title": "Plan", "href": str(env / "repo" / "docs" / "plan.md")}]
    if fields:
        cli(env, ["--project", SLUG, "config", "set", "--json", json.dumps(fields)])


def _errors(env, scenario, **kw):
    return check_state.run_check(env, scenario, **kw)


def test_plan_6_tasks__complete_board__passes(env):
    # arrange
    _plan_board(env)
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert errors == []
    assert (env / "snapshot-plan-6-tasks.json").exists()


@pytest.mark.parametrize("flaw,expected", [
    ({"violation": False}, "без нарушения"),
    ({"brief": False}, "project_brief"),
    ({"links": False}, "links"),
])
def test_plan_6_tasks__missing_part__fails(env, flaw, expected):
    # arrange
    _plan_board(env, **flaw)
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any(expected in e for e in errors), errors
    assert not (env / "snapshot-plan-6-tasks.json").exists()


def test_plan_6_tasks__wrong_deps__fails(env):
    # arrange
    _project(env)
    _task(env, "PAY-1", "Add storage module")
    _task(env, "PAY-2", "Add CLI commands on top of storage")
    _task(env, "PAY-3", "Add help output and an end-to-end test")
    _task(env, "PAY-4", "Add logging configuration")
    _task(env, "PAY-5", "Update README usage section")
    _task(env, "PAY-6", "Research export formats", kind="research")
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("зависимости «commands»" in e for e in errors), errors
    assert any("зависимости «e2e»" in e for e in errors), errors


def test_plan_6_tasks__empty_done_when__fails(env):
    # arrange
    _plan_board(env)
    cli(env, ["--project", SLUG, "update", "PAY-4", "--done-when", " "])
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("пустой done_when" in e for e in errors), errors


def test_plan_6_tasks__rerun_same_state__passes(env):
    # arrange
    _plan_board(env)
    assert _errors(env, "plan-6-tasks") == []
    # act
    errors = _errors(env, "1", rerun=True)
    # assert
    assert errors == []


def test_plan_6_tasks__rerun_with_duplicate__fails(env):
    # arrange
    _plan_board(env)
    assert _errors(env, "plan-6-tasks") == []
    _task(env, "PAY-7", "Add storage module again")
    # act
    errors = _errors(env, "plan-6-tasks", rerun=True)
    # assert
    assert any("число задач" in e for e in errors), errors


def test_plan_6_tasks__task_moved_from_not_started__fails(env):
    # arrange
    _plan_board(env)
    cli(env, ["--project", SLUG, "status", "PAY-4", "in_progress"])
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("сдвинуты" in e for e in errors), errors


def _ready_board(env, with_benchmark_deps=True):
    _project(env)
    _task(env, "PAY-1", "Add parser module")
    _task(env, "PAY-2", "Add validator module", ["PAY-1"])
    _task(env, "PAY-3", "Add reporter module", ["PAY-2"])
    _task(env, "PAY-4", "Update docs")
    _task(env, "PAY-5", "Add benchmark script", ["PAY-1", "PAY-3"] if with_benchmark_deps else ["PAY-1"])


def test_ready_list__five_tasks_with_deps__passes(env):
    # arrange
    _ready_board(env)
    # act / assert
    assert _errors(env, "ready-list") == []


def test_ready_list__missing_dependency__fails(env):
    # arrange
    _ready_board(env, with_benchmark_deps=False)
    # act
    errors = _errors(env, "2")
    # assert
    assert any("зависимости «benchmark»" in e for e in errors), errors


def test_ready_list__extra_task__fails(env):
    # arrange
    _ready_board(env)
    _task(env, "PAY-6", "Split of something")
    # act
    errors = _errors(env, "ready-list")
    # assert
    assert any("задач 6" in e for e in errors), errors


@pytest.fixture
def existing_env(tmp_path, capsys):
    env_dir = tmp_path / "env"
    assert make_env.main([str(env_dir), "--existing-project"]) == 0
    yield env_dir
    cli(env_dir, ["stop"], check=False)


def test_resume_board__server_running_project_unchanged__passes(existing_env):
    # arrange
    cli(existing_env, ["serve", "--detach"])
    # act / assert
    assert _errors(existing_env, "resume-board") == []


def test_resume_board__server_stopped__fails(existing_env):
    # act
    errors = _errors(existing_env, "3")
    # assert
    assert any("не запущен" in e for e in errors), errors


def test_resume_board__project_changed__fails(existing_env):
    # arrange
    cli(existing_env, ["serve", "--detach"])
    task = {"key": "DEMO-2", "title": "Extra", "description": "d", "done_when": "x", "repo": "repo", "kind": "code",
            "deps": []}
    cli(existing_env, ["--project", "demo", "add-task", "--json", json.dumps(task)])
    # act
    errors = _errors(existing_env, "resume-board")
    # assert
    assert any("изменился" in e for e in errors), errors


def _jira_board(env, auth):
    tracker = {"type": "jira", "url": "https://jira.example.com", "project": "PRJ", "issue_type": "Task",
               "epic": None, "components": [], "labels": [], "auth": auth,
               "statuses": {"Open": None, "Done": "done"}}
    _project(env, {"tracker": tracker})


def test_secret_token__env_name_only__passes(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    # act / assert
    assert _errors(env, "secret-token") == []


def test_secret_token__cookie_file_path__passes(env):
    # arrange
    _jira_board(env, {"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"})
    # act / assert
    assert _errors(env, "4") == []


def test_secret_token__secret_in_home__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    (env / "home" / "notes.txt").write_text("JIRA_TOKEN=abc123SECRETxyz", encoding="utf-8")
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("секрет найден" in e for e in errors), errors


def test_secret_token__secret_in_transcript__fails(env, tmp_path):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    transcript = tmp_path / "t.txt"
    transcript.write_text("you sent abc123SECRETxyz", encoding="utf-8")
    # act
    errors = _errors(env, "secret-token", transcript=transcript)
    # assert
    assert any("повторил секрет" in e for e in errors), errors


def test_secret_token__tracker_not_configured__fails(env):
    # arrange
    _project(env)
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("не настроен" in e for e in errors), errors


def test_secret_token__wrong_env_name__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "OTHER_TOKEN"})
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("JIRA_TOKEN" in e for e in errors), errors


def test_make_env__fresh_env__repo_has_identity_and_plan_and_home_is_empty(env):
    # act
    import subprocess

    email = subprocess.run(["git", "-C", str(env / "repo"), "config", "--local", "user.email"],
                           capture_output=True, text=True).stdout.strip()
    # assert
    assert email and (env / "repo" / "docs" / "plan.md").exists() and (env / "repo" / "app.py").exists()
    assert list((env / "home").iterdir()) == []


def test_check_state__unknown_scenario__raises(env):
    # act / assert
    with pytest.raises(ValueError):
        check_state.run_check(env, "nope")


def _append_event(env, slug, type_):
    path = env / "home" / "projects" / slug / "events.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seq = max((e["seq"] for e in lines), default=0) + 1
    event = {"seq": seq, "type": type_, "at": "2026-01-01T00:00:00+00:00", "data": {}}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def test_plan_6_tasks__turn_started__fails(env):
    # arrange
    _plan_board(env)
    _append_event(env, SLUG, "turn_started")
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("ходы агентов" in e for e in errors), errors


def test_ready_list__turn_started__fails(env):
    # arrange
    _ready_board(env)
    _append_event(env, SLUG, "turn_started")
    # act
    errors = _errors(env, "ready-list")
    # assert
    assert any("ходы агентов" in e for e in errors), errors


def test_plan_6_tasks__stage_not_decomposition__fails(env):
    # arrange
    _plan_board(env)
    cli(env, ["--project", SLUG, "stage", "tracker"])
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("ожидалась decomposition" in e for e in errors), errors


def test_plan_6_tasks__unrelated_violation_on_long_task__fails(env):
    # arrange
    _plan_board(env, violation=False)
    cli(env, ["--project", SLUG, "update", "PAY-2", "--rule-violations-json",
              json.dumps([{"rule": "Naming", "text": "Title is vague."}])])
    # act
    errors = _errors(env, "plan-6-tasks")
    # assert
    assert any("без нарушения" in e for e in errors), errors


def test_plan_6_tasks__plain_recheck_keeps_first_snapshot(env):
    # arrange
    _plan_board(env)
    assert _errors(env, "plan-6-tasks") == []
    snapshot = env / "snapshot-plan-6-tasks.json"
    first = snapshot.read_text(encoding="utf-8")
    cli(env, ["--project", SLUG, "update", "PAY-4", "--title", "Add logging setup"])
    # act
    assert _errors(env, "plan-6-tasks") == []
    # assert
    assert snapshot.read_text(encoding="utf-8") == first


def test_plan_6_tasks__rerun_with_renamed_task__fails(env):
    # arrange
    _plan_board(env)
    assert _errors(env, "plan-6-tasks") == []
    cli(env, ["--project", SLUG, "update", "PAY-4", "--title", "Add logging setup"])
    # act
    errors = _errors(env, "plan-6-tasks", rerun=True)
    # assert
    assert any("названия" in e for e in errors), errors


def test_plan_6_tasks__rerun_with_recreated_keys__fails(env):
    # arrange
    _plan_board(env)
    assert _errors(env, "plan-6-tasks") == []
    snapshot = env / "snapshot-plan-6-tasks.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["keys"] = ["OLD-%d" % i for i in range(6)]
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    # act
    errors = _errors(env, "plan-6-tasks", rerun=True)
    # assert
    assert any("ключи" in e for e in errors), errors


def test_resume_board__turn_started__fails(existing_env):
    # arrange
    cli(existing_env, ["serve", "--detach"])
    _append_event(existing_env, "demo", "turn_started")
    # act
    errors = _errors(existing_env, "resume-board")
    # assert
    assert any("ходы агентов" in e for e in errors), errors


def test_secret_token__secret_in_env_root_file__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    (env / "notes.txt").write_text("abc123SECRETxyz", encoding="utf-8")
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("секрет найден" in e and "notes.txt" in e for e in errors), errors


def test_secret_token__secret_in_repo_file__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    (env / "repo" / "x.md").write_text("abc123SECRETxyz", encoding="utf-8")
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("секрет найден" in e and "x.md" in e for e in errors), errors


def _commit(env, message, files=None, remove=None):
    repo = env / "repo"
    for name, text in (files or {}).items():
        (repo / name).write_text(text, encoding="utf-8")
    for name in remove or []:
        (repo / name).unlink()
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=check_state.git_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, capture_output=True,
                   env=check_state.git_env())


def test_secret_token__secret_committed_then_removed__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    _commit(env, "add config", files={"cfg.txt": "token=abc123SECRETxyz"})
    _commit(env, "drop config", remove=["cfg.txt"])
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("истории git" in e for e in errors), errors


def test_secret_token__secret_in_commit_message__fails(env):
    # arrange
    _jira_board(env, {"type": "token_env", "env": "JIRA_TOKEN"})
    _commit(env, "use token abc123SECRETxyz", files={"a.txt": "x"})
    # act
    errors = _errors(env, "secret-token")
    # assert
    assert any("истории git" in e for e in errors), errors


def test_make_env__poisoned_home__does_not_touch_real_board_home(tmp_path, capsys, monkeypatch):
    # arrange
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n[core]\n\thooksPath = /nonexistent\n",
                                          encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    env_dir = tmp_path / "env"
    # act
    assert make_env.main([str(env_dir), "--existing-project"]) == 0
    cli(env_dir, ["stop"], check=False)
    # assert
    assert not (fake_home / ".agent-board").exists()
    assert (env_dir / "home" / "projects" / "demo").is_dir()
    assert ".agent-board" not in os.listdir(str(fake_home))
