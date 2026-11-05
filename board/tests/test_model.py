from __future__ import annotations

from agent_board.config import ProjectConfig
from agent_board.model import Question, Status, Task


def test_task_roundtrip_preserves_nested_questions():
    t = Task(key="PAY-101", title="Схема", deps=["PAY-100"],
             questions=[Question(id="q1", text="Как?", options=["A", "B"], context="…", recommended="A")])
    assert Task.from_dict(t.to_dict()) == t


def test_from_dict_ignores_unknown_keys():
    assert Task.from_dict({"key": "K", "title": "T", "junk": 1}).key == "K"


def test_status_labels():
    assert Status.WAITING_ANSWER.label == "Ждёт ответа"
    assert Status("queued") is Status.QUEUED


def test_config_defaults():
    c = ProjectConfig(slug="pay", title="Платежи")
    assert c.chains is True and c.max_agents == 3 and c.tracker == {"type": "none"} and c.review == {"tool": "crit"}


def test_task_execution_fields_roundtrip_and_defaults():
    # arrange
    t = Task(key="K", title="T")
    assert (t.agent_state, t.turn_error, t.last_turn_at, t.harness) == ("idle", "", "", "")
    t.agent_state, t.turn_error, t.last_turn_at, t.harness = "error", "boom", "2026-01-01", "codex"
    # act / assert
    assert Task.from_dict(t.to_dict()) == t


def test_config_execution_defaults():
    # arrange / act
    c = ProjectConfig(slug="pay", title="Платежи")
    # assert
    assert c.harness == {"type": "claude", "permission_mode": "auto"}
    assert c.branch_template == "{key}" and c.global_max_agents == 6
    assert c.project_brief == "" and c.project_session_id is None
