from __future__ import annotations

import pytest
from conftest import make_task as mk

from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board.model import Question, Status
from agent_board.store import ProjectStore

NOW = "2026-01-01T00:00:00+00:00"


def _board(chains, on_event=None):
    s = ProjectStore("pay")
    s.create(ProjectConfig(slug="pay", title="Платежи", chains=chains))
    return Board(s, clock=lambda: NOW, on_event=on_event)


@pytest.fixture
def board(tmp_home):
    return _board(True)


@pytest.fixture
def board_strict(tmp_home):
    return _board(False)


def flat(events):
    return [dict(type=e.type, **e.data) for e in events]


def wake(key, reason, **extra):
    return dict(type="agent_wake", key=key, reason=reason, **extra)


def test_add_task_ready_emits_task_added_and_wake(board):
    ev = board.add_task(mk("A"), "user")
    assert flat(ev) == [dict(type="task_added", key="A"), wake("A", "ready")]


def test_add_task_with_open_dep_does_not_wake(board):
    board.add_task(mk("A"), "user")
    ev = board.add_task(mk("B", deps=["A"]), "user")
    assert [e.type for e in ev] == ["task_added"]


def test_add_task_duplicate_key_rejected(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.add_task(mk("A"), "user")


def test_add_task_with_cycle_leaves_state_unchanged(board):
    board.add_task(mk("A"), "user")
    last = board.snapshot()["last_seq"]
    with pytest.raises(ValueError):
        board.add_task(mk("B", deps=["B"]), "user")
    with pytest.raises(ValueError):
        board.add_task(mk("C", deps=["Z"]), "user")
    snap = board.snapshot()
    assert [t["key"] for t in snap["tasks"]] == ["A"]
    assert snap["last_seq"] == last


def test_snapshot_contains_display_status_and_open_deps(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    snap = board.snapshot()
    b = [t for t in snap["tasks"] if t["key"] == "B"][0]
    assert b["display_status"] == "blocked" and b["open_deps"] == ["A"]
    assert snap["config"]["slug"] == "pay" and snap["proposals"] == []
    assert snap["project_unread"] == 0


def test_approving_dep_unblocks_dependent_with_chains(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    ev = flat(board.transition("A", Status.APPROVED, "agent"))
    assert dict(type="task_unblocked", key="B") in ev
    assert wake("B", "unblocked") in ev


def test_no_unblock_on_approved_without_chains(board_strict):
    board_strict.add_task(mk("A"), "user")
    board_strict.add_task(mk("B", deps=["A"]), "user")
    ev = board_strict.transition("A", Status.APPROVED, "agent")
    assert [e.type for e in ev] == ["status_changed"]
    ev = flat(board_strict.transition("A", Status.DONE, "user"))
    assert wake("B", "unblocked") in ev


def test_dependent_woken_only_once(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.transition("A", Status.APPROVED, "agent")
    ev = flat(board.transition("A", Status.DONE, "user"))
    assert wake("B", "unblocked") not in ev


def test_dependent_with_other_open_dep_not_unblocked(board):
    for k in ("A", "C"):
        board.add_task(mk(k), "user")
    board.add_task(mk("B", deps=["A", "C"]), "user")
    ev = board.transition("A", Status.APPROVED, "agent")
    assert [e.type for e in ev] == ["status_changed"]


def test_status_changed_event_payload(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.transition("A", Status.IN_PROGRESS, "agent"))
    assert ev == [dict(type="status_changed", key="A", to="in_progress", actor="agent", **{"from": "not_started"})]


def test_return_to_in_progress_wakes_agent_with_from(board):
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_REVIEW, "agent")
    ev = flat(board.transition("A", Status.IN_PROGRESS, "user"))
    assert wake("A", "returned", **{"from": "in_review"}) in ev


def test_agent_return_to_in_progress_does_not_wake(board):
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_REVIEW, "agent")
    ev = board.transition("A", Status.IN_PROGRESS, "agent")
    assert [e.type for e in ev] == ["status_changed"]


def test_cancel_removes_from_deps_and_unblocks(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    ev = flat(board.transition("A", Status.CANCELLED, "user"))
    assert dict(type="agent_stop", key="A") in ev
    assert dict(type="deps_changed", key="B", deps=[]) in ev
    assert dict(type="task_unblocked", key="B") in ev
    assert wake("B", "unblocked") in ev
    assert board.snapshot()["tasks"][1]["deps"] == []


def test_agent_cannot_mark_done(board):
    board.add_task(mk("A"), "user")
    for status in (Status.DONE, Status.SENT, Status.CANCELLED):
        with pytest.raises(PermissionError):
            board.transition("A", status, "agent")
    assert board.snapshot()["tasks"][0]["status"] == "not_started"


def test_transition_unknown_actor_rejected(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.transition("A", Status.DONE, "project_agent")


def test_update_task_whitelisted_fields(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.update_task("A", "agent", summary="s", branch="b"))
    assert ev == [dict(type="task_updated", key="A", fields=["branch", "summary"])]
    t = board.snapshot()["tasks"][0]
    assert t["summary"] == "s" and t["branch"] == "b"


def test_update_task_rejects_unknown_field(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.update_task("A", "agent", status="done")
    with pytest.raises(ValueError):
        board.update_task("A", "agent", summary="x", deps=[])
    assert board.snapshot()["tasks"][0]["summary"] == ""


def test_set_deps_validates_and_unblocks(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    with pytest.raises(ValueError):
        board.set_deps("A", ["B"], "user")
    ev = flat(board.set_deps("B", [], "user"))
    assert ev == [dict(type="deps_changed", key="B", deps=[]), dict(type="task_unblocked", key="B"), wake("B", "unblocked")]


def test_ask_assigns_id_and_waits_for_answer(board):
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    ev = flat(board.ask("A", Question(id="", text="Как?", options=["x", "y"])))
    assert ev[0] == dict(type="question_asked", key="A", question_id="q1")
    assert ev[1]["type"] == "status_changed" and ev[1]["to"] == "waiting_answer"
    board.ask("A", Question(id="", text="Ещё?"))
    ids = [q["id"] for q in board.snapshot()["tasks"][0]["questions"]]
    assert ids == ["q1", "q2"]


def test_answer_last_question_returns_to_in_progress(board):
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    board.ask("A", Question(id="", text="1?"))
    board.ask("A", Question(id="", text="2?"))
    ev = flat(board.answer("A", "q1", "x", "почему"))
    assert dict(type="question_answered", key="A", question_id="q1") in ev
    assert wake("A", "answer") in ev
    assert board.snapshot()["tasks"][0]["status"] == "waiting_answer"
    ev = flat(board.answer("A", "q2", None, "ок"))
    assert dict(type="status_changed", key="A", to="in_progress", actor="system", **{"from": "waiting_answer"}) in ev
    assert wake("A", "returned", **{"from": "waiting_answer"}) not in ev
    thread = board.store.read_thread("A")
    assert [(m.author, m.question_id) for m in thread] == [("user", "q1"), ("user", "q2")]
    assert board.snapshot()["tasks"][0]["questions"][0]["answer"] == {"choice": "x", "comment": "почему"}


def test_answer_unknown_question_rejected(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(KeyError):
        board.answer("A", "q9", None, "x")


def test_user_message_wakes_task_agent_and_project_message_wakes_project_agent(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.post_message("A", "user", "привет"))
    assert ev[0]["type"] == "message_posted" and ev[0]["thread"] == "A"
    assert wake("A", "message") in ev
    ev = flat(board.post_message("_project", "user", "план?"))
    assert wake("_project", "message") in ev
    ev = flat(board.post_message("A", "project_agent", "совет"))
    assert wake("A", "message") in ev
    ev = flat(board.post_message("A", "agent", "готово"))
    assert not [e for e in ev if e["type"] == "agent_wake"]


def test_unread_counts_non_user_messages_and_mark_read_resets(board):
    board.add_task(mk("A"), "user")
    board.post_message("A", "agent", "1")
    board.post_message("A", "agent", "2")
    board.post_message("A", "user", "3")
    assert board.snapshot()["tasks"][0]["unread"] == 2
    board.mark_read("A")
    assert board.snapshot()["tasks"][0]["unread"] == 0


def test_project_unread_lives_in_meta(board):
    board.post_message("_project", "project_agent", "1")
    board.post_message("_project", "user", "2")
    assert board.snapshot()["project_unread"] == 1
    assert board.store.load_meta()["project_unread"] == 1
    board.mark_read("_project")
    assert board.snapshot()["project_unread"] == 0


def test_post_message_unknown_thread_rejected(board):
    with pytest.raises(KeyError):
        board.post_message("ZZ", "user", "x")


def test_on_event_called_for_every_event_in_order(tmp_home):
    seen = []
    b = _board(True, on_event=seen.append)
    ev = b.add_task(mk("A"), "user")
    assert [e.seq for e in seen] == [e.seq for e in ev] == [1, 2]
