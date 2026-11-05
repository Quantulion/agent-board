from __future__ import annotations

import pytest
from conftest import NOW, flat, make_board, make_task as mk, wake

from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board.model import Question, Status
from agent_board.store import ProjectStore

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


def test_snapshot_contains_project_agent_state(board):
    # arrange
    board.update_config({"project_session_id": "p-1"})
    board.update_meta({"project_agent_state": "error", "project_turn_error": "упал"})
    # act
    snap = board.snapshot()
    # assert
    assert snap["project_agent"] == {"state": "error", "turn_error": "упал", "session_id": "p-1", "parked": False}


def test_snapshot_project_agent_defaults(board):
    assert board.snapshot()["project_agent"] == {"state": "idle", "turn_error": "", "session_id": None, "parked": False}


def test_snapshot_project_agent_parked_flag(board):
    # arrange
    board.update_meta({"project_agent_parked": True})
    # act
    snap = board.snapshot()
    # assert
    assert snap["project_agent"]["parked"] is True


def test_agent_parked_is_runner_field(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    with pytest.raises(PermissionError):
        board.update_task("A", "user", {"agent_parked": True})
    board.update_task("A", "system", {"agent_parked": True})
    # assert
    assert board.snapshot()["tasks"][0]["agent_parked"] is True


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
    ev = flat(board.transition("A", Status.APPROVED, "system"))
    assert dict(type="task_unblocked", key="B") in ev
    assert wake("B", "unblocked") in ev


def test_no_unblock_on_approved_without_chains(board_strict):
    board_strict.add_task(mk("A"), "user")
    board_strict.add_task(mk("B", deps=["A"]), "user")
    ev = board_strict.transition("A", Status.APPROVED, "system")
    assert [e.type for e in ev] == ["status_changed"]
    ev = flat(board_strict.transition("A", Status.DONE, "user"))
    assert wake("B", "unblocked") in ev


def test_dependent_woken_only_once(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.transition("A", Status.APPROVED, "system")
    ev = flat(board.transition("A", Status.DONE, "user"))
    assert wake("B", "unblocked") not in ev


def test_dependent_with_other_open_dep_not_unblocked(board):
    for k in ("A", "C"):
        board.add_task(mk(k), "user")
    board.add_task(mk("B", deps=["A", "C"]), "user")
    ev = board.transition("A", Status.APPROVED, "system")
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


def test_agent_cannot_approve_and_dependent_is_not_woken(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.transition("A", Status.IN_REVIEW, "agent")
    before = board.store.last_seq()
    # act
    with pytest.raises(PermissionError):
        board.transition("A", Status.APPROVED, "agent")
    # assert
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW
    assert board.store.read_events(before) == []


def test_transition_unknown_actor_rejected(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.transition("A", Status.DONE, "bogus")


def test_update_task_whitelisted_fields(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.update_task("A", "agent", {"summary": "s", "report": "r.md"}))
    assert ev == [dict(type="task_updated", key="A", fields=["report", "summary"])]
    t = board.snapshot()["tasks"][0]
    assert t["summary"] == "s" and t["report"] == "r.md"


@pytest.mark.parametrize("actor", ["agent", "user", "tracker", "project_agent"])
@pytest.mark.parametrize("field", ["worktree", "branch", "base_branch", "session_id", "agent_state",
                                   "turn_error", "last_turn_at", "harness", "turn_process"])
def test_update_task_runner_fields_only_by_system(board, actor, field):
    # arrange
    board.add_task(mk("A"), "user")
    before = board.store.last_seq()
    # act
    with pytest.raises(PermissionError):
        board.update_task("A", actor, {"summary": "s", field: "/tmp/forged"})
    # assert
    assert board.store.load_tasks()["A"].summary == ""
    assert board.store.read_events(before) == []


def test_update_task_rejects_unknown_field(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.update_task("A", "agent", {"status": "done"})
    with pytest.raises(ValueError):
        board.update_task("A", "agent", {"summary": "x", "deps": []})
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
    assert board.snapshot()["tasks"][0]["questions"][0]["answer"] == {"choice": "x", "comment": "почему", "at": NOW}


def test_answer_unknown_question_rejected(board):
    board.add_task(mk("A"), "user")
    with pytest.raises(KeyError):
        board.answer("A", "q9", None, "x")


def test_user_message_wakes_task_agent_and_project_message_wakes_project_agent(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.post_message("A", "user", "привет"))
    assert ev[0]["type"] == "message_posted" and ev[0]["thread"] == "A"
    assert wake("A", "message", message_id=ev[0]["message_id"], by="user") in ev
    ev = flat(board.post_message("_project", "user", "план?"))
    assert wake("_project", "message", message_id=ev[0]["message_id"], by="user") in ev
    ev = flat(board.post_message("A", "project_agent", "совет"))
    assert wake("A", "message", message_id=ev[0]["message_id"], by="project_agent") in ev
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


def test_snapshot_project_thread_size_follows_messages(board):
    assert board.snapshot()["project_thread_size"] == 0
    board.post_message("_project", "user", "1")
    board.post_message("_project", "project_agent", "2")
    assert board.snapshot()["project_thread_size"] == 2


def test_post_message_unknown_thread_rejected(board):
    with pytest.raises(KeyError):
        board.post_message("ZZ", "user", "x")


def test_on_event_called_for_every_event_in_order(tmp_home):
    seen = []
    b = make_board(True, on_event=seen.append)
    ev = b.add_task(mk("A"), "user")
    assert [e.seq for e in seen] == [e.seq for e in ev] == [1, 2]


def test_clearing_external_blocker_unblocks_task(board):
    ev = board.add_task(mk("A", external_blocker="ждём доступ"), "user")
    assert [e.type for e in ev] == ["task_added"]
    ev = flat(board.update_task("A", "user", {"external_blocker": None}))
    assert dict(type="task_unblocked", key="A") in ev
    assert wake("A", "unblocked") in ev


def test_setting_external_blocker_does_not_wake(board):
    board.add_task(mk("A"), "user")
    ev = board.update_task("A", "user", {"external_blocker": "ждём"})
    assert [e.type for e in ev] == ["task_updated"]


def test_transition_accepts_string_status(board):
    board.add_task(mk("A"), "user")
    ev = flat(board.transition("A", "done", "user"))
    assert ev[0]["to"] == "done"


def test_transition_invalid_status_leaves_state_unchanged(board):
    board.add_task(mk("A"), "user")
    last = board.snapshot()["last_seq"]
    with pytest.raises(ValueError):
        board.transition("A", "nope", "user")
    snap = board.snapshot()
    assert snap["tasks"][0]["status"] == "not_started" and snap["last_seq"] == last


def test_project_agent_cannot_change_graph(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    last = board.snapshot()["last_seq"]
    with pytest.raises(PermissionError):
        board.add_task(mk("C"), "project_agent")
    with pytest.raises(PermissionError):
        board.set_deps("B", ["A"], "project_agent")
    with pytest.raises(PermissionError):
        board.transition("A", Status.CANCELLED, "project_agent")
    snap = board.snapshot()
    assert [t["key"] for t in snap["tasks"]] == ["A", "B"] and snap["last_seq"] == last


# ---------- финальная волна правок ----------

def test_add_task_key_differing_only_in_case_rejected(board):
    # arrange
    board.add_task(mk("PAY-1"), "user")

    # act / assert
    with pytest.raises(ValueError):
        board.add_task(mk("pay-1"), "user")
    assert list(board.store.load_tasks()) == ["PAY-1"]
    assert board.store.load_tasks()["PAY-1"].title == "PAY-1"


def test_add_task_key_starting_with_underscore_rejected(board):
    # act / assert
    with pytest.raises(ValueError):
        board.add_task(mk("_project"), "user")
    with pytest.raises(ValueError):
        board.add_task(mk("_x"), "user")
    assert board.store.load_tasks() == {}


def test_add_task_forces_not_started_status(board):
    # act
    board.add_task(mk("A", status=Status.DONE), "user")

    # assert
    assert board.store.load_tasks()["A"].status == Status.NOT_STARTED


def test_transition_to_stored_blocked_rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    last = board.snapshot()["last_seq"]

    # act / assert
    with pytest.raises(ValueError) as ei:
        board.transition("A", Status.BLOCKED, "user")
    assert "external_blocker" in str(ei.value)
    snap = board.snapshot()
    assert snap["tasks"][0]["status"] == "not_started" and snap["last_seq"] == last


def test_agent_message_to_project_with_from_task_wakes_project_agent(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    ev = flat(board.post_message("_project", "agent", "нужна помощь", from_task="A"))

    # assert
    assert ev[0]["type"] == "message_posted"
    assert wake("_project", "escalation", by="agent", **{"from": "A"}) in ev


def test_agent_message_to_project_without_from_task_does_not_wake(board):
    # act
    ev = board.post_message("_project", "agent", "просто так")

    # assert
    assert [e.type for e in ev] == ["message_posted"]


def test_escalation_from_unknown_task_rejected(board):
    # act / assert
    with pytest.raises(KeyError):
        board.post_message("_project", "agent", "x", from_task="NOPE")


def test_from_task_ignored_outside_project_thread(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    ev = board.post_message("A", "agent", "готово", from_task="A")

    # assert
    assert [e.type for e in ev] == ["message_posted"]


def test_user_message_wake_carries_message_id(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    ev = flat(board.post_message("A", "user", "привет"))

    # assert
    assert ev[1] == wake("A", "message", message_id=ev[0]["message_id"], by="user")


def test_answer_stores_time_and_emits_message_posted(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    board.ask("A", Question(id="", text="Как?"))

    # act
    ev = flat(board.answer("A", "q1", "x", "так"))

    # assert
    msg = board.store.read_thread("A")[0]
    assert dict(type="message_posted", thread="A", message_id=msg.id, author="user") in ev
    assert board.snapshot()["tasks"][0]["questions"][0]["answer"] == {"choice": "x", "comment": "так", "at": NOW}


def test_update_task_takes_fields_dict_and_rejects_key_field(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act / assert
    with pytest.raises(ValueError):
        board.update_task("A", "agent", {"key": "B"})
    with pytest.raises(ValueError):
        board.update_task("A", "agent", {"actor": "x"})
    with pytest.raises(ValueError):
        board.update_task("A", "agent", "summary")
    assert list(board.store.load_tasks()) == ["A"]


def test_events_use_board_clock(board):
    # act
    ev = board.add_task(mk("A"), "user")

    # assert
    assert [e.at for e in ev] == [NOW, NOW]
    assert {e.at for e in board.store.read_events()} == {NOW}


def test_user_manual_start_wakes_agent(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    ev = flat(board.transition("A", Status.IN_PROGRESS, "user"))

    # assert
    assert wake("A", "manual") in ev


def test_user_manual_start_of_blocked_task_wakes_agent(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")

    # act
    ev = flat(board.transition("B", Status.IN_PROGRESS, "user"))

    # assert
    assert wake("B", "manual") in ev


def test_manual_start_with_session_or_by_agent_does_not_wake(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.update_task("A", "system", {"session_id": "s1"})

    # act
    ev_session = board.transition("A", Status.IN_PROGRESS, "user")
    ev_agent = board.transition("B", Status.IN_PROGRESS, "agent")

    # assert
    assert [e.type for e in ev_session] == ["status_changed"]
    assert [e.type for e in ev_agent] == ["status_changed"]


def test_uncancel_reenters_readiness(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.add_task(mk("C", deps=["B"]), "user")
    board.transition("A", Status.CANCELLED, "user")
    board.transition("C", Status.CANCELLED, "user")

    # act
    ready = flat(board.transition("A", Status.NOT_STARTED, "user"))
    blocked = flat(board.transition("C", Status.NOT_STARTED, "user"))

    # assert
    assert wake("A", "ready") in ready
    assert [e["type"] for e in blocked] == ["status_changed"]


def test_snapshot_last_seq_does_not_reread_log(board, monkeypatch):
    # arrange
    board.add_task(mk("A"), "user")
    monkeypatch.setattr(board.store, "read_events", lambda since=0: pytest.fail("журнал перечитан"))

    # act / assert
    assert board.snapshot()["last_seq"] == 2


def test_event_vocabulary_covers_every_emitted_event():
    # arrange
    import re
    from pathlib import Path
    from agent_board import board as board_mod

    source = Path(board_mod.__file__).read_text(encoding="utf-8")

    # act
    emitted = set(re.findall(r'_emit\("([a-z_]+)"', source))
    reasons = set(re.findall(r'"reason": "([a-z_]+)"', source))

    # assert
    assert emitted and emitted <= set(board_mod.EVENT_TYPES)
    assert reasons <= set(board_mod.WAKE_REASONS)
    assert set(board_mod.WAKE_REASONS) == {
        "ready", "unblocked", "returned", "answer", "message", "proposal_rejected", "escalation", "manual",
        "rebase", "interrupted", "queued_restart"}


def test_new_task_from_dict_builds_task_from_creatable_fields():
    # arrange
    from agent_board.board import new_task_from_dict

    # act
    t = new_task_from_dict({"key": "A", "title": "a", "estimate": 1.5, "deps": [], "kind": "code"})

    # assert
    assert (t.key, t.title, t.estimate, t.status) == ("A", "a", 1.5, Status.NOT_STARTED)


@pytest.mark.parametrize("raw", [
    {"key": "A", "title": "a", "status": "in_progress"},
    {"key": "A", "title": "a", "external_blocker": "x"},
    {"key": "A", "title": "a", "estimate": None, "deps": None},
    {"key": 1, "title": "a"},
    {"title": "a"},
    "A",
])
def test_new_task_from_dict_rejects_invalid_input(raw):
    # arrange
    from agent_board.board import new_task_from_dict

    # act / assert
    with pytest.raises(ValueError):
        new_task_from_dict(raw)


def test_update_task_accepts_agent_state(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    board.update_task("A", "system", {"agent_state": "running", "turn_error": "", "last_turn_at": "t", "harness": "fake"})
    # assert
    t = [x for x in board.snapshot()["tasks"] if x["key"] == "A"][0]
    assert t["agent_state"] == "running" and t["harness"] == "fake"


def test_reapproving_dep_wakes_dependent_with_session_as_rebase(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    board.transition("A", Status.APPROVED, "user")
    board.transition("A", Status.IN_PROGRESS, "user")
    # act
    ev = flat(board.transition("A", Status.APPROVED, "user"))
    # assert
    assert dict(type="agent_wake", key="B", reason="rebase", **{"from": "A"}) in ev


def test_dep_done_wakes_dependent_with_session_as_rebase(board_strict):
    # arrange
    board_strict.add_task(mk("A"), "user")
    board_strict.add_task(mk("B", deps=["A"]), "user")
    board_strict.update_task("B", "system", {"session_id": "s1"})
    # act
    ev = flat(board_strict.transition("A", Status.DONE, "user"))
    # assert
    assert dict(type="agent_wake", key="B", reason="rebase", **{"from": "A"}) in ev


def _rebases(ev):
    return [(e["key"], e["from"]) for e in ev if e.get("reason") == "rebase"]


def test_rebase_not_sent_without_session_or_for_closed_dependent(board):
    # arrange: D с сессией — положительный контроль; B без сессии, C отменена
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.add_task(mk("C", deps=["A"]), "user")
    board.add_task(mk("D", deps=["A"]), "user")
    board.update_task("C", "system", {"session_id": "s1"})
    board.update_task("D", "system", {"session_id": "s2"})
    board.transition("C", Status.CANCELLED, "user")
    # act
    ev = flat(board.transition("A", Status.APPROVED, "user"))
    # assert
    assert _rebases(ev) == [("D", "A")]
    assert wake("B", "unblocked") in ev


def test_rebase_not_sent_for_system_actor(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    # act
    by_system = flat(board.transition("A", Status.APPROVED, "system"))
    board.transition("A", Status.IN_PROGRESS, "user")
    by_user = flat(board.transition("A", Status.APPROVED, "user"))
    # assert
    assert _rebases(by_system) == []
    assert _rebases(by_user) == [("B", "A")]


def test_rebase_sent_for_tracker_actor(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    # act
    ev = flat(board.transition("A", Status.APPROVED, "tracker"))
    # assert
    assert _rebases(ev) == [("B", "A")]


def test_rebase_not_sent_to_transitive_dependent(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.add_task(mk("C", deps=["B"]), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    board.update_task("C", "system", {"session_id": "s2"})
    # act
    ev = flat(board.transition("A", Status.APPROVED, "user"))
    # assert
    assert _rebases(ev) == [("B", "A")]


def test_record_appends_event_and_notifies(tmp_home):
    # arrange
    seen = []
    board = make_board(True, on_event=seen.append)
    # act
    ev = board.record("turn_started", {"key": "A"})
    # assert
    assert ev.type == "turn_started" and ev.data == {"key": "A"}
    assert seen == [ev]


def test_record_unknown_type_raises(board):
    # arrange / act / assert
    with pytest.raises(ValueError):
        board.record("nope", {})


def test_snapshot_terminal_command_via_hook_only_for_tasks_with_session(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    assert all(t["terminal_command"] is None for t in board.snapshot()["tasks"])
    board.terminal_command_for = lambda t: "claude --resume %s" % t.session_id
    # act
    tasks = {t["key"]: t for t in board.snapshot()["tasks"]}
    # assert
    assert tasks["A"]["terminal_command"] is None
    assert tasks["B"]["terminal_command"] == "claude --resume s1"


def test_snapshot_survives_raising_terminal_command_hook(board, capsys):
    # arrange
    board.add_task(mk("A"), "user")
    board.update_task("A", "system", {"session_id": "s1"})
    board.terminal_command_for = lambda t: 1 / 0
    # act
    tasks = board.snapshot()["tasks"]
    # assert
    assert tasks[0]["terminal_command"] is None
    assert "terminal_command_for" in capsys.readouterr().err


def test_transition_if_skips_when_status_differs(board):
    # arrange
    board.add_task(mk("T1"), "user")
    before = board.store.last_seq()
    # act
    events = board.transition_if("T1", Status.IN_PROGRESS, Status.QUEUED, "system")
    # assert
    assert events == [] and board.store.last_seq() == before
    assert board.store.load_tasks()["T1"].status == Status.NOT_STARTED


def test_transition_if_applies_when_status_matches(board):
    # arrange
    board.add_task(mk("T1"), "user")
    # act
    events = board.transition_if("T1", Status.NOT_STARTED, Status.QUEUED, "system")
    # assert
    assert events and board.store.load_tasks()["T1"].status == Status.QUEUED


# --- стадии проекта ---

def test_set_stage_walks_all_stages_and_emits_stage_changed(board):
    # act
    events = [board.set_stage(stage, "user") for stage in ("tracker", "work", "done")]
    # assert
    assert [flat(ev) for ev in events] == [
        [dict(type="stage_changed", **{"from": "decomposition", "to": "tracker"})],
        [dict(type="stage_changed", **{"from": "tracker", "to": "work"})],
        [dict(type="stage_changed", **{"from": "work", "to": "done"})],
    ]
    assert board.store.load_config().stage == "done"


def test_set_stage_skips_tracker(board):
    # act
    ev = board.set_stage("work", "user")
    # assert
    assert flat(ev) == [dict(type="stage_changed", **{"from": "decomposition", "to": "work"})]
    assert board.store.load_config().stage == "work"


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system", "tracker"])
def test_set_stage_by_non_user_is_forbidden(board, actor):
    # act / assert
    with pytest.raises(PermissionError):
        board.set_stage("work", actor)
    assert board.store.load_config().stage == "decomposition"
    assert board.snapshot()["last_seq"] == 0


@pytest.mark.parametrize("path, to", [
    ((), "done"),
    ((), "decomposition"),
    (("tracker",), "decomposition"),
    (("tracker",), "done"),
    (("work",), "tracker"),
    (("work",), "decomposition"),
    (("work", "done"), "work"),
    ((), "nonsense"),
])
def test_set_stage_rejects_disallowed_transition(board, path, to):
    # arrange
    for stage in path:
        board.set_stage(stage, "user")
    before = board.store.load_config().stage
    # act / assert
    with pytest.raises(ValueError):
        board.set_stage(to, "user")
    assert board.store.load_config().stage == before


# --- снимок: данные для карточки ---

def test_snapshot_task_without_thread_has_empty_summary(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    task = board.snapshot()["tasks"][0]
    # assert
    assert task["thread_size"] == 0 and task["last_message"] is None and task["open_questions"] == 0


def test_snapshot_exposes_thread_size_last_message_and_open_questions(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.post_message("A", "user", "первое")
    board.post_message("A", "agent", "я" * 300)
    board.ask("A", Question(id="", text="q1"))
    board.ask("A", Question(id="", text="q2"))
    board.answer("A", board.store.load_tasks()["A"].questions[0].id, None, "ок")
    # act
    tasks = {t["key"]: t for t in board.snapshot()["tasks"]}
    # assert
    a = tasks["A"]
    assert a["thread_size"] == len(board.store.read_thread("A")) >= 2
    assert a["last_message"]["author"] != "" and a["last_message"]["at"] == NOW
    assert a["open_questions"] == 1
    assert tasks["B"]["thread_size"] == 0


def test_snapshot_last_message_text_is_cut_to_200_chars(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.post_message("A", "agent", "я" * 300)
    # act
    last = board.snapshot()["tasks"][0]["last_message"]
    # assert
    assert last == {"author": "agent", "text": "я" * 200, "at": NOW}


def test_snapshot_thread_summary_follows_new_messages(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.post_message("A", "user", "раз")
    assert board.snapshot()["tasks"][0]["thread_size"] == 1
    # act
    board.post_message("A", "user", "два")
    # assert
    task = board.snapshot()["tasks"][0]
    assert task["thread_size"] == 2 and task["last_message"]["text"] == "два"
