from __future__ import annotations

import pytest
from conftest import flat, make_task as mk, wake

from agent_board.config import ProjectConfig
from agent_board.messages import msg
from agent_board.model import Status
from agent_board.prompts import project_prompt


def _put(board, key, status, **kw):
    board.add_task(mk(key, **kw), "user")
    if status != Status.NOT_STARTED:
        board.transition(key, status, "user")


@pytest.fixture
def allowed(board):
    board.update_config({"project_agent_status": True})
    return board


@pytest.mark.parametrize("prev, to", [
    (Status.IN_REVIEW, Status.DONE),
    (Status.APPROVED, Status.DONE),
    (Status.SENT, Status.DONE),
    (Status.IN_PROGRESS, Status.NOT_STARTED),
    (Status.NOT_STARTED, Status.CANCELLED),
    (Status.CANCELLED, Status.NOT_STARTED),
    (Status.NOT_STARTED, Status.IN_PROGRESS),
])
def test_transition__project_agent_allowed__should_change_status_and_note_in_thread(allowed, prev, to):
    # arrange
    _put(allowed, "A", prev)
    # act
    ev = flat(allowed.transition("A", to, "project_agent"))
    # assert
    assert allowed.store.load_tasks()["A"].status == to
    assert dict(type="status_changed", key="A", actor="project_agent", **{"from": prev.value, "to": to.value}) in ev
    note = allowed.store.read_thread("A")[-1]
    assert note.author == "system" and prev.value in note.text and to.value in note.text


def test_transition__project_agent_returns_to_work__should_wake_task_agent(allowed):
    # arrange
    _put(allowed, "A", Status.IN_REVIEW)
    # act
    ev = flat(allowed.transition("A", Status.IN_PROGRESS, "project_agent"))
    # assert
    assert wake("A", "returned", **{"from": "in_review"}) in ev


def test_transition__project_agent_closes_dependency__should_wake_dependents_for_rebase(allowed):
    # arrange
    _put(allowed, "A", Status.APPROVED)
    _put(allowed, "B", Status.IN_PROGRESS, deps=["A"])
    allowed.update_task("B", "system", {"session_id": "s-1"})
    # act
    ev = flat(allowed.transition("A", Status.DONE, "project_agent"))
    # assert
    assert wake("B", "rebase", **{"from": "A"}) in ev


@pytest.mark.parametrize("to", [Status.APPROVED, Status.SENT, Status.IN_REVIEW])
def test_transition__project_agent_to_user_only_status__should_be_rejected(allowed, to):
    # arrange
    _put(allowed, "A", Status.IN_REVIEW if to != Status.IN_REVIEW else Status.IN_PROGRESS)
    before = allowed.store.last_seq()
    # act / assert
    with pytest.raises(PermissionError):
        allowed.transition("A", to, "project_agent")
    assert allowed.store.read_events(before) == []


@pytest.mark.parametrize("to", [Status.DONE, Status.IN_PROGRESS, Status.NOT_STARTED, Status.CANCELLED])
def test_transition__project_agent_without_setting__should_be_rejected(board, to):
    # arrange
    _put(board, "A", Status.IN_REVIEW)
    before = board.store.last_seq()
    # act / assert
    with pytest.raises(PermissionError, match=str(msg("board.graph_via_proposals"))):
        board.transition("A", to, "project_agent")
    assert board.store.read_events(before) == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("to", [Status.DONE, Status.APPROVED, Status.SENT, Status.CANCELLED, Status.NOT_STARTED])
def test_transition__task_agent_beyond_work_and_review__should_be_rejected(board, enabled, to):
    # arrange
    board.update_config({"project_agent_status": enabled})
    _put(board, "A", Status.IN_REVIEW)
    before = board.store.last_seq()
    # act / assert
    with pytest.raises(PermissionError):
        board.transition("A", to, "agent")
    assert board.store.read_events(before) == []


def test_update_task__project_agent_sets_and_clears_external_blocker__should_block_and_unblock(allowed):
    # arrange
    allowed.add_task(mk("A"), "user")
    # act
    allowed.update_task("A", "project_agent", {"external_blocker": "ждём доступ"})
    blocked = allowed.snapshot()["tasks"][0]["display_status"]
    ev = flat(allowed.update_task("A", "project_agent", {"external_blocker": None}))
    # assert
    assert blocked == "blocked"
    assert wake("A", "unblocked") in ev


@pytest.mark.parametrize("actor, enabled", [("project_agent", False), ("agent", True)])
def test_update_task__external_blocker_by_not_allowed_actor__should_be_rejected(board, actor, enabled):
    # arrange
    board.update_config({"project_agent_status": enabled})
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(PermissionError):
        board.update_task("A", actor, {"external_blocker": "x"})
    assert board.store.load_tasks()["A"].external_blocker is None


@pytest.mark.parametrize("enabled", [False, True])
def test_project_prompt__status_section__should_follow_setting(enabled):
    # arrange
    config = ProjectConfig(slug="p", title="P", project_agent_status=enabled)
    # act
    text = project_prompt(config, {}, "agent-board")
    # assert
    assert ("agent-board status <KEY> done" in text) is enabled
