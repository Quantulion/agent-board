from __future__ import annotations

import re

import pytest
from conftest import NOW, flat, make_board, make_task as mk, wake

from agent_board.board import RUNNER_FIELDS, Board
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
    assert snap["project_agent"] == {"state": "error", "turn_error": "упал", "session_id": "p-1", "parked": False,
                                     "wait_reason": "", "turn_process": None}


def test_snapshot_project_agent_defaults(board):
    assert board.snapshot()["project_agent"] == {"state": "idle", "turn_error": "", "session_id": None, "parked": False,
                                                   "wait_reason": "", "turn_process": None}


def test_snapshot_project_agent_running_turn_start(board):
    # arrange
    record = {"pgid": 42, "started": "Thu Nov  5 19:09:21 2026", "mark": "p-1", "at": "2026-11-05T16:09:21+00:00"}
    board.update_meta({"project_agent_state": "running", "project_turn_process": record})
    # act
    pa = board.snapshot()["project_agent"]
    # assert
    assert pa["state"] == "running" and pa["turn_process"] == record


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


@pytest.mark.parametrize("actor, field", [("agent", f) for f in sorted(RUNNER_FIELDS)]
                         + [(a, "worktree") for a in ("user", "tracker", "project_agent")])
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


def test_update_task__tracker_link_by_user__is_saved(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    board.update_task("A", "user", {"tracker_key": "PRJ-123", "tracker_url": "https://tracker.example/PRJ-123"})
    # assert
    task = board.store.load_tasks()["A"]
    assert (task.tracker_key, task.tracker_url) == ("PRJ-123", "https://tracker.example/PRJ-123")


def test_update_task__tracker_link_cleared_by_user__is_removed(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.update_task("A", "user", {"tracker_key": "PRJ-123", "tracker_url": "https://tracker.example/PRJ-123"})
    # act
    board.update_task("A", "user", {"tracker_key": None, "tracker_url": None})
    # assert
    task = board.store.load_tasks()["A"]
    assert (task.tracker_key, task.tracker_url) == (None, None)


@pytest.mark.parametrize("actor", ["agent", "project_agent", "tracker"])
def test_update_task__tracker_link_cleared_by_non_user__rejected(board, actor):
    # arrange
    board.add_task(mk("A"), "user")
    board.update_task("A", "user", {"tracker_key": "PRJ-123", "tracker_url": "https://tracker.example/PRJ-123"})
    # act / assert
    with pytest.raises(PermissionError):
        board.update_task("A", actor, {"tracker_key": None, "tracker_url": None})
    task = board.store.load_tasks()["A"]
    assert (task.tracker_key, task.tracker_url) == ("PRJ-123", "https://tracker.example/PRJ-123")


@pytest.mark.parametrize("actor", ["agent", "project_agent", "tracker"])
@pytest.mark.parametrize("field", ["tracker_key", "tracker_url"])
def test_update_task__tracker_link_by_non_user__rejected(board, actor, field):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(PermissionError):
        board.update_task("A", actor, {field: "https://tracker.example/X-1"})
    assert board.store.load_tasks()["A"].tracker_key is None


@pytest.mark.parametrize("fields", [{"tracker_key": ""}, {"tracker_key": "PRJ 1"}, {"tracker_key": 5},
                                    {"tracker_key": "X" * 101}, {"tracker_url": "file:///etc/passwd"},
                                    {"tracker_url": "javascript:alert(1)"}])
def test_update_task__tracker_link_invalid__rejected(board, fields):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError):
        board.update_task("A", "user", fields)
    task = board.store.load_tasks()["A"]
    assert (task.tracker_key, task.tracker_url) == (None, None)


JIRA_TRACKER = {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
                "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}
GITHUB_TRACKER = {"type": "github", "repo": "owner/name", "auth": {"type": "token_env", "env": "GITHUB_TOKEN"}}


@pytest.mark.parametrize("tracker, key", [(JIRA_TRACKER, "PROJ-123"), (JIRA_TRACKER, "AB2_X-7"),
                                          (GITHUB_TRACKER, "#42")])
def test_update_task__tracker_key_matches_tracker_format__is_saved(board, tracker, key):
    # arrange
    board.update_config({"tracker": tracker})
    board.add_task(mk("A"), "user")
    # act
    board.update_task("A", "user", {"tracker_key": key})
    # assert
    assert board.store.load_tasks()["A"].tracker_key == key


@pytest.mark.parametrize("tracker, key, example", [
    (JIRA_TRACKER, "proj-123", "PROJ-123"), (JIRA_TRACKER, "PROJ123", "PROJ-123"), (JIRA_TRACKER, "#42", "PROJ-123"),
    (JIRA_TRACKER, "PROJ-0", "PROJ-123"), (JIRA_TRACKER, "1PROJ-5", "PROJ-123"),
    (GITHUB_TRACKER, "42", "#123"), (GITHUB_TRACKER, "PROJ-1", "#123"), (GITHUB_TRACKER, "#0", "#123"),
    (GITHUB_TRACKER, "#4a", "#123"),
])
def test_update_task__tracker_key_wrong_format__rejected_with_example(board, tracker, key, example):
    # arrange
    board.update_config({"tracker": tracker})
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError, match="должен быть вида %s" % re.escape(example)):
        board.update_task("A", "user", {"tracker_key": key})
    assert board.store.load_tasks()["A"].tracker_key is None


def test_update_task__tracker_key_without_tracker__any_format_saved(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    board.update_task("A", "user", {"tracker_key": "task/17"})
    # assert
    assert board.store.load_tasks()["A"].tracker_key == "task/17"


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


def test_answer_non_string_choice_rejected_without_changes(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    board.ask("A", Question(id="", text="1?"))

    # act
    with pytest.raises(ValueError):
        board.answer("A", "q1", 2, "ок")

    # assert: вопрос остался без ответа, повторный ответ возможен
    assert board.snapshot()["tasks"][0]["questions"][0]["answer"] is None
    assert board.store.read_thread("A") == []
    board.answer("A", "q1", "x", "ок")


def test_user_message_wakes_task_agent_and_project_message_wakes_project_agent(board):
    board.add_task(mk("A"), "user")
    board.set_stage("work", "user")
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


@pytest.mark.parametrize("closed", [Status.DONE, Status.CANCELLED])
def test_transition__agent_on_closed_task__rejected_and_status_kept(board, closed):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", closed, "user")
    last = board.snapshot()["last_seq"]
    # act / assert
    for to in (Status.IN_REVIEW, Status.IN_PROGRESS):
        with pytest.raises(PermissionError):
            board.transition("A", to, "agent")
    snap = board.snapshot()
    assert snap["tasks"][0]["status"] == closed.value and snap["last_seq"] == last


def test_transition__user_reopens_done_task__allowed(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.DONE, "user")
    # act
    board.transition("A", Status.IN_PROGRESS, "user")
    # assert
    assert board.snapshot()["tasks"][0]["status"] == "in_progress"


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
    board.set_stage("work", "user")

    # act
    ev = flat(board.post_message("A", "user", "привет"))

    # assert
    assert ev[1] == wake("A", "message", message_id=ev[0]["message_id"], by="user")


def test_post_message__user_comment_on_task_during_decomposition__should_wake_project_agent_not_task_agent(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    ev = flat(board.post_message("A", "user", "разбей задачу"))

    # assert
    assert ev[0] == dict(type="message_posted", thread="A", message_id=ev[0]["message_id"], author="user")
    assert [e for e in ev if e["type"] == "agent_wake"] == [
        wake("_project", "message", task="A", message_id=ev[0]["message_id"], by="user")]
    assert [m.text for m in board.store.read_thread("A")] == ["разбей задачу"]


@pytest.mark.parametrize("stage", ["tracker", "work"])
def test_post_message__user_comment_on_task_after_decomposition__should_wake_task_agent(board, stage):
    # arrange
    board.add_task(mk("A"), "user")
    board.set_stage(stage, "user")

    # act
    ev = flat(board.post_message("A", "user", "привет"))

    # assert
    assert [e for e in ev if e["type"] == "agent_wake"] == [
        wake("A", "message", message_id=ev[0]["message_id"], by="user")]


def test_post_message__agent_messages_during_decomposition__should_keep_their_wakes(board):
    # arrange
    board.add_task(mk("A"), "user")

    # act
    from_project = flat(board.post_message("A", "project_agent", "совет"))
    to_project = flat(board.post_message("_project", "user", "план?"))

    # assert
    assert [e["key"] for e in from_project if e["type"] == "agent_wake"] == ["A"]
    assert [(e["key"], "task" in e) for e in to_project if e["type"] == "agent_wake"] == [("_project", False)]


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
        "rebase", "interrupted", "queued_restart", "review", "retry"}


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
    {"key": None, "title": "a"},
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


def test_return_to_work__transition_rejected__should_not_leave_message(board, monkeypatch):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_REVIEW, "agent")

    def reject(key, to, actor):
        raise ValueError("переход отклонён")

    monkeypatch.setattr(board, "_check_transition", reject)
    # act
    with pytest.raises(ValueError):
        board.return_to_work("A", "user", "доделай")
    # assert
    assert board.store.read_thread("A") == []
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW


# --- настройки проекта: проверка значений ---

@pytest.mark.parametrize("fields, message", [
    ({"ship": {"command": 'git push "{branch}"'}}, "кавычки"),
    ({"ship": {"command": "run $'x' {key}"}}, "$'"),
    ({"ship": {"command": "cat <<EOF\n{title}\nEOF"}}, "<<"),
    ({"ship": {"command": "run {secret}"}}, "плейсхолдер"),
    ({"ship": {"command": "true", "timeout": 0}}, "timeout"),
    ({"ship": {"command": None, "on_approve": True}}, "ship.command"),
    ({"ship": {"command": "  ", "on_approve": True}}, "ship.command"),
    ({"ship": {"command": "true", "on_approve": "yes"}}, "on_approve"),
    ({"ship": {"command": "true", "on_failure": "retry"}}, "on_failure"),
    ({"tracker": dict(JIRA_TRACKER, auth={"type": "token_env", "env": "X", "token": "s3cret"})}, "auth"),
    ({"tracker": dict(JIRA_TRACKER, password="x")}, "Недопустимые ключи"),
    ({"tracker": dict(JIRA_TRACKER, statuses={"Review": "approved"})}, "approved"),
    ({"links": [{"title": "x", "href": "javascript:alert(1)"}]}, "ссылк"),
    ({"links": "https://x"}, "links"),
    ({"repos": [{"name": "app", "path": "/nonexistent/repo/path"}]}, "не найден"),
    ({"repos": [{"name": "app", "path": "relative/path"}]}, "абсолютн"),
    ({"harness": {"type": "gpt"}}, "харнесс"),
    ({"review": {"tool": "gerrit"}}, "ревью"),
    ({"max_agents": 0}, "max_agents"),
    ({"global_max_agents": "6"}, "global_max_agents"),
    ({"chains": "yes"}, "chains"),
    ({"auto_approve": "yes"}, "auto_approve"),
    ({"branch_template": "feature/x"}, "{key}"),
    ({"branch_template": "{key}-{title}"}, "branch_template"),
    ({"resources": [{"name": "db"}, {"name": "db"}]}, "db"),
    ({"title": ""}, "title"),
    ({"unknown": 1}, "unknown"),
    ({"stage": "work"}, "stage"),
    ({"slug": "other"}, "slug"),
    ({"project_session_id": "x"}, "project_session_id"),
])
def test_set_config__invalid__is_rejected_and_not_saved(board, fields, message):
    # arrange
    project_file = board.store.root / "project.json"
    before = project_file.read_bytes()
    last = board.store.last_seq()
    # act / assert
    with pytest.raises(ValueError, match=re.escape(message)):
        board.set_config("user", fields)
    with pytest.raises(ValueError, match=re.escape(message)):
        board.set_config("user", fields, dry_run=True)
    assert project_file.read_bytes() == before
    assert board.store.last_seq() == last


# --- настройки проекта: проверки вне блокировки ---

def test_set_config__slow_git_check__does_not_hold_board_lock(board, git_repo, monkeypatch):
    # arrange
    import threading

    from agent_board import config as config_module

    started, release = threading.Event(), threading.Event()
    real_check = config_module._git_work_tree

    def slow_check(path):
        started.set()
        release.wait(10)
        return real_check(path)

    monkeypatch.setattr(config_module, "_git_work_tree", slow_check)
    result = {}
    worker = threading.Thread(target=lambda: result.update(
        r=board.set_config("user", {"repos": [{"name": "app", "path": str(git_repo)}]})))
    worker.start()
    assert started.wait(10)
    added = {}
    writer = threading.Thread(target=lambda: added.update(e=board.add_task(mk("A"), "user")))
    # act
    writer.start()
    writer.join(5)
    finished_while_checking = not writer.is_alive()
    release.set()
    worker.join(10)
    # assert
    assert finished_while_checking and added["e"]
    assert result["r"]["saved"] is True and board.store.load_config().repos[0]["name"] == "app"


def test_set_config__stored_config_with_outdated_fields__other_field_can_change(board):
    # arrange: репозиторий удалён с диска, команда отправки записана по старым правилам
    cfg = board.store.load_config()
    cfg.repos = [{"name": "old", "path": "/nonexistent/repo/path"}]
    cfg.ship = {"command": 'git push "{branch}"', "timeout": 600}
    board.store.save_config(cfg)
    # act
    result = board.set_config("user", {"max_agents": 2})
    # assert
    stored = board.store.load_config()
    assert result["saved"] is True and stored.max_agents == 2 and stored.repos[0]["name"] == "old"


def test_set_config__unchanged_repos__are_not_checked_on_disk(board, git_repo, monkeypatch):
    # arrange
    from agent_board import config as config_module

    cfg = board.store.load_config()
    old = {"name": "old", "path": "/nonexistent/repo/path"}
    cfg.repos = [old]
    board.store.save_config(cfg)
    checked = []
    real_check = config_module._git_work_tree
    monkeypatch.setattr(config_module, "_git_work_tree", lambda path: checked.append(path) or real_check(path))
    # act
    board.set_config("user", {"repos": [old, {"name": "app", "path": str(git_repo)}]})
    # assert
    assert checked == [str(git_repo)]
    assert [r["name"] for r in board.store.load_config().repos] == ["old", "app"]


def test_set_config__changed_repo_path__is_checked(board):
    # arrange
    cfg = board.store.load_config()
    cfg.repos = [{"name": "app", "path": "/nonexistent/repo/path"}]
    board.store.save_config(cfg)
    # act / assert
    with pytest.raises(ValueError, match="не найден"):
        board.set_config("user", {"repos": [{"name": "app", "path": "/nonexistent/other/path"}]})


def test_set_config__merged_result_breaks_structure__is_rejected(board):
    # arrange: файл поправлен вручную — неизвестный харнесс
    cfg = board.store.load_config()
    cfg.harness = {"type": "gpt"}
    board.store.save_config(cfg)
    # act / assert
    with pytest.raises(ValueError, match="харнесс"):
        board.set_config("user", {"max_agents": 2})


def test_set_config__field_changes_between_check_and_save__retries(board, monkeypatch):
    # arrange: пока шла проверка, это же поле поменяли один раз
    from agent_board import board as board_module

    real = board_module.validate_fields
    calls = []

    def racing(config, names, previous):
        calls.append(1)
        if len(calls) == 1:
            stored = board.store.load_config()
            stored.agent_rules = "чужая правка"
            board.store.save_config(stored)
        real(config, names, previous)

    monkeypatch.setattr(board_module, "validate_fields", racing)
    # act
    board.set_config("user", {"agent_rules": "моя правка"})
    # assert
    assert len(calls) == 2 and board.store.load_config().agent_rules == "моя правка"


def test_set_config__field_keeps_changing__conflict(board, monkeypatch):
    # arrange
    from agent_board import board as board_module

    real = board_module.validate_fields
    counter = []

    def racing(config, names, previous):
        counter.append(1)
        stored = board.store.load_config()
        stored.agent_rules = "чужая правка %d" % len(counter)
        board.store.save_config(stored)
        real(config, names, previous)

    monkeypatch.setattr(board_module, "validate_fields", racing)
    # act / assert
    with pytest.raises(board_module.ConfigConflict):
        board.set_config("user", {"agent_rules": "моя правка"})
    assert len(counter) == 3


@pytest.mark.parametrize("name", ["to_dict", "from_dict", "__class__", "nope"])
def test_set_config__unknown_field__is_rejected(board, name):
    # act / assert
    with pytest.raises(ValueError, match="Неизвестное поле"):
        board.set_config("user", {name: 1})


# --- задачи добавляет только пользователь ---

@pytest.mark.parametrize("actor", ["agent", "project_agent", "tracker", "system"])
def test_add_task__not_user__is_forbidden(board, actor):
    # act / assert
    with pytest.raises(PermissionError):
        board.add_task(mk("A"), actor)
    assert board.store.load_tasks() == {}


# --- устаревшая команда отправки ---

def test_legacy_ship_command__moved_to_ship_and_journaled_once(tmp_home):
    # arrange
    import json

    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи"))
    raw = json.loads((store.root / "project.json").read_text(encoding="utf-8"))
    raw["ship_command"] = "git push origin {branch}"
    raw["ship"] = {"command": None, "timeout": 120}
    (store.root / "project.json").write_text(json.dumps(raw), encoding="utf-8")
    # act
    Board(store, clock=lambda: NOW)
    Board(store, clock=lambda: NOW)
    # assert
    stored = json.loads((store.root / "project.json").read_text(encoding="utf-8"))
    assert stored["ship"] == {"command": "git push origin {branch}", "timeout": 120} and "ship_command" not in stored
    migrated = [e.data for e in store.read_events() if e.type == "config_migrated"]
    assert migrated == [{"field": "ship_command", "to": "ship.command", "moved": True}]


def test_legacy_ship_command__ship_already_set__legacy_dropped():
    # act
    cfg = ProjectConfig.from_dict({"slug": "pay", "title": "x", "ship_command": "old {key}",
                                   "ship": {"command": "new {key}", "timeout": 60}})
    # assert
    assert cfg.ship == {"command": "new {key}", "timeout": 60}


def test_set_config__outdated_value_resent_unchanged__is_accepted(board):
    # arrange: клиент присылает настройки целиком, вместе со старой командой отправки
    cfg = board.store.load_config()
    cfg.ship = {"command": 'git push "{branch}"', "timeout": 600}
    board.store.save_config(cfg)
    # act
    result = board.set_config("user", {"ship": {"command": 'git push "{branch}"', "timeout": 600}, "max_agents": 2})
    # assert
    assert result["saved"] is True and board.store.load_config().max_agents == 2


def test_legacy_ship_command__save_fails__board_still_works(tmp_home, monkeypatch, capsys):
    # arrange
    import json

    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи"))
    raw = json.loads((store.root / "project.json").read_text(encoding="utf-8"))
    raw["ship_command"] = "git push origin {branch}"
    (store.root / "project.json").write_text(json.dumps(raw), encoding="utf-8")

    def broken(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(store, "save_config", broken)
    # act
    board = Board(store, clock=lambda: NOW)
    # assert
    assert store.load_config().ship["command"] == "git push origin {branch}"
    assert board.snapshot()["tasks"] == [] and "read-only" in capsys.readouterr().err


def _with_repos(board, *names):
    config = board.store.load_config()
    config.repos = [{"name": n, "path": "/tmp/" + n, "base": "main"} for n in names]
    board.store.save_config(config)


def test_add_task__repo_not_in_project_repos__rejected(board):
    # arrange
    _with_repos(board, "api")
    # act / assert
    with pytest.raises(ValueError, match="worker"):
        board.add_task(mk("A", repo="worker"), "user")
    assert board.snapshot()["tasks"] == []


def test_add_task__repo_in_project_repos_or_no_repos_configured__accepted(board):
    # arrange
    board.add_task(mk("A", repo="anything"), "user")
    _with_repos(board, "api")
    # act
    board.add_task(mk("B", repo="api"), "user")
    board.add_task(mk("C"), "user")
    # assert
    assert [t["key"] for t in board.snapshot()["tasks"]] == ["A", "B", "C"]


def test_propose_add_task__repo_not_in_project_repos__rejected(board):
    # arrange
    _with_repos(board, "api")
    # act / assert
    with pytest.raises(ValueError, match="worker"):
        board.propose("add_task", {"task": {"key": "A", "title": "a", "repo": "worker"}}, "")


def test_set_deps__task_agent__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    # act / assert
    with pytest.raises(PermissionError):
        board.set_deps("B", ["A"], "agent")
    assert board.snapshot()["tasks"][1]["deps"] == []


def test_auto_approve__agent_moves_code_task_to_review__approved_by_system_with_note(board):
    # arrange
    board.update_config({"auto_approve": True})
    board.add_task(mk("A"), "user")
    board.add_task(mk("B", deps=["A"]), "user")
    board.update_task("B", "system", {"session_id": "s1"})
    # act
    ev = flat(board.transition("A", Status.IN_REVIEW, "agent"))
    # assert
    assert dict(type="status_changed", key="A", to="approved", actor="system", auto=True,
                **{"from": "in_review"}) in ev
    assert dict(type="agent_wake", key="B", reason="rebase", **{"from": "A"}) in ev
    assert board.store.load_tasks()["A"].status == Status.APPROVED
    texts = [m.text for m in board.store.read_thread("A") if m.author == "system"]
    assert "Одобрено автоматически (auto_approve)" in texts


def test_auto_approve__research_task__stays_in_review(board):
    # arrange
    board.update_config({"auto_approve": True})
    board.add_task(mk("R", kind="research"), "user")
    # act
    ev = board.transition("R", Status.IN_REVIEW, "agent")
    # assert
    assert [e.type for e in ev] == ["status_changed"]
    assert board.store.load_tasks()["R"].status == Status.IN_REVIEW


def test_auto_approve__disabled__agent_review_stays_in_review(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    ev = board.transition("A", Status.IN_REVIEW, "agent")
    # assert
    assert [e.type for e in ev] == ["status_changed"]
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW


@pytest.mark.parametrize("actor", ["user", "system", "tracker"])
def test_auto_approve__review_set_not_by_agent__not_approved(board, actor):
    # arrange: возврат на ревью после неудачного коммита делает system — повторного одобрения быть не должно
    board.update_config({"auto_approve": True})
    board.add_task(mk("A"), "user")
    # act
    board.transition("A", Status.IN_REVIEW, actor)
    # assert
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW


def test_add_task__without_priority__is_normal(board):
    # arrange / act
    board.add_task(mk("A"), "user")
    # assert
    assert board.store.load_tasks()["A"].priority == "normal"


@pytest.mark.parametrize("actor", ["user", "project_agent"])
def test_update_task__priority_by_user_or_project_agent__saved_with_event(board, actor):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    ev = board.update_task("A", actor, {"priority": "high"})
    # assert
    assert board.store.load_tasks()["A"].priority == "high"
    changed = [e for e in ev if e.type == "priority_changed"]
    assert [e.data for e in changed] == [{"key": "A", "from": "normal", "to": "high", "actor": actor}]


def test_update_task__same_priority__no_priority_event(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act
    ev = board.update_task("A", "user", {"priority": "normal"})
    # assert
    assert [e.type for e in ev] == ["task_updated"]


@pytest.mark.parametrize("actor", ["agent", "tracker"])
def test_update_task__priority_by_task_agent__rejected(board, actor):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(PermissionError):
        board.update_task("A", actor, {"priority": "high"})
    assert board.store.load_tasks()["A"].priority == "normal"


@pytest.mark.parametrize("value", ["urgent", "", None, 1])
def test_update_task__unknown_priority__rejected(board, value):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError):
        board.update_task("A", "user", {"priority": value})


def test_new_task_from_dict__priority__validated():
    # arrange
    from agent_board.board import new_task_from_dict
    # act / assert
    assert new_task_from_dict({"key": "A", "title": "a", "priority": "low"}).priority == "low"
    with pytest.raises(ValueError):
        new_task_from_dict({"key": "A", "title": "a", "priority": "urgent"})


# --- текст задачи ---

@pytest.mark.parametrize("actor", ["agent", "project_agent", "tracker"])
@pytest.mark.parametrize("fields", [{"title": "t"}, {"description": "d"}, {"done_when": "ничего"}, {"estimate": 9}])
def test_update_task__task_text_by_non_user__rejected(board, actor, fields):
    # arrange
    board.add_task(mk("A"), "user")
    before = board.store.last_seq()
    # act / assert
    with pytest.raises(PermissionError):
        board.update_task("A", actor, fields)
    task = board.store.load_tasks()["A"]
    assert (task.title, task.description, task.done_when, task.estimate) == ("A", "", "", None)
    assert board.store.read_events(before) == []


# --- ревью ---

def _in_review(board, key="A", **extra):
    board.add_task(mk(key, **extra), "user")
    board.transition(key, Status.IN_REVIEW, "agent")


def test_request_review__in_review_task__records_event(board):
    # arrange
    _in_review(board)
    # act
    events = board.request_review("A", "user")
    # assert
    assert flat(events) == [dict(type="review_requested", key="A")]


def test_request_review__outside_in_review__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError, match="ревью"):
        board.request_review("A", "user")


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system"])
def test_request_review__not_user__forbidden(board, actor):
    # arrange
    _in_review(board)
    # act / assert
    with pytest.raises(PermissionError):
        board.request_review("A", actor)


def test_request_review__round_already_open__rejected(board):
    # arrange
    _in_review(board)
    board.update_task("A", "system", {"review": {"round": 1, "state": "open"}})
    # act / assert
    with pytest.raises(ValueError):
        board.request_review("A", "user")


def test_request_review__no_review_tool__rejected(board):
    # arrange
    board.update_config({"review": {"tool": "none"}})
    _in_review(board)
    # act / assert
    with pytest.raises(ValueError):
        board.request_review("A", "user")


def test_approve__by_user__moves_to_approved(board):
    # arrange
    _in_review(board)
    # act
    events = board.approve("A", "user")
    # assert
    assert [e.data for e in events if e.type == "status_changed"] == [
        {"key": "A", "from": "in_review", "to": "approved", "actor": "user"}]
    assert board.store.load_tasks()["A"].status == Status.APPROVED


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system"])
def test_approve__not_user__forbidden_and_status_kept(board, actor):
    # arrange
    _in_review(board)
    # act / assert
    with pytest.raises(PermissionError):
        board.approve("A", actor)
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW


def test_approve__outside_in_review__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError):
        board.approve("A", "user")


def test_return_to_work__with_comment__posts_message_and_wakes_once_with_it(board):
    # arrange
    _in_review(board)
    # act
    events = board.return_to_work("A", "user", "поправь тесты")
    # assert
    assert [e.type for e in events] == ["message_posted", "status_changed", "agent_wake"]
    wake_data = events[2].data
    assert wake_data["reason"] == "returned" and wake_data["from"] == "in_review"
    assert wake_data["message_id"] == events[0].data["message_id"]
    assert board.store.load_tasks()["A"].status == Status.IN_PROGRESS
    assert [(m.author, m.text) for m in board.store.read_thread("A")] == [("user", "поправь тесты")]


@pytest.mark.parametrize("actor, text, error", [
    ("user", "  ", ValueError),
    ("user", None, ValueError),
    ("agent", "x", PermissionError),
])
def test_return_to_work__invalid__rejected_and_status_kept(board, actor, text, error):
    # arrange
    _in_review(board)
    # act / assert
    with pytest.raises(error):
        board.return_to_work("A", actor, text)
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW
    assert board.store.read_thread("A") == []


def test_return_to_work__from_in_progress__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    # act / assert
    with pytest.raises(ValueError):
        board.return_to_work("A", "user", "x")


# --- отправка ---

def _approved(board, key="A", **extra):
    _in_review(board, key, **extra)
    board.approve(key, "user")


def _with_stored_ship(board, command):
    config = board.store.load_config()
    config.ship = {"command": command, "timeout": 5}
    board.store.save_config(config)


def test_request_ship__several_tasks__queued_in_dependency_order(board):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    _approved(board, "A")
    _approved(board, "B", deps=["A"])
    # act
    events = board.request_ship(["B", "A"], "user")
    # assert
    assert flat(events) == [dict(type="ship_requested", keys=["A", "B"])]
    assert board.store.load_tasks()["A"].ship["state"] == "queued"


@pytest.mark.parametrize("command, message", [
    (None, "команда отправки"),
    ("echo {nope}", "{nope}"),
    ('run "{title}"', "в кавычки"),
])
def test_request_ship__bad_command__rejected_and_not_queued(board, command, message):
    # arrange
    if command is not None:
        _with_stored_ship(board, command)
    _approved(board)
    # act / assert
    with pytest.raises(ValueError, match=re.escape(message)):
        board.request_ship(["A"], "user")
    assert board.store.load_tasks()["A"].ship is None


def test_request_ship__task_not_approved__rejected(board):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    board.add_task(mk("A"), "user")
    # act / assert
    with pytest.raises(ValueError):
        board.request_ship(["A"], "user")
    assert board.store.load_tasks()["A"].ship is None


def test_request_ship__unshipped_dependency_outside_request__rejected(board):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    _approved(board, "A")
    _approved(board, "B", deps=["A"])
    # act / assert
    with pytest.raises(ValueError, match="сначала отправьте A"):
        board.request_ship(["B"], "user")


def test_request_ship__second_request_while_running__rejected(board):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    _approved(board)
    board.request_ship(["A"], "user")
    # act / assert
    with pytest.raises(ValueError, match="уже отправляется"):
        board.request_ship(["A"], "user")


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system"])
def test_request_ship__not_user__forbidden(board, actor):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    _approved(board)
    # act / assert
    with pytest.raises(PermissionError):
        board.request_ship(["A"], actor)
    assert board.store.load_tasks()["A"].ship is None


@pytest.mark.parametrize("keys", [[], "A", ["A", "A"], [1]])
def test_request_ship__bad_keys__rejected(board, keys):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    _approved(board)
    # act / assert
    with pytest.raises(ValueError):
        board.request_ship(keys, "user")


def test_request_ship__unknown_task__not_found(board):
    # arrange
    board.update_config({"ship": {"command": "true"}})
    # act / assert
    with pytest.raises(KeyError):
        board.request_ship(["NOPE"], "user")


# --- повтор упавшего хода и перенос на базу ---

def _failed(board, key="A", error="обрыв соединения"):
    board.add_task(mk(key), "user")
    board.set_stage("work", "user")
    board.update_task(key, "system", {"agent_state": "error", "turn_error": error})


def test_retry__failed_task__records_retry_wake_with_error(board):
    # arrange
    _failed(board)
    # act
    events = board.retry("A", "user")
    # assert
    assert flat(events) == [wake("A", "retry", error="обрыв соединения")]


def test_retry__idle_task__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.set_stage("work", "user")
    # act / assert
    with pytest.raises(ValueError, match="idle"):
        board.retry("A", "user")


@pytest.mark.parametrize("key, actor", [("A", "agent"), ("_project", "project_agent")])
def test_retry__by_agent__forbidden(board, key, actor):
    # arrange
    _failed(board)
    board.update_meta({"project_agent_state": "error", "project_turn_error": "упал CLI"})
    # act / assert
    with pytest.raises(PermissionError):
        board.retry(key, actor)


def test_retry__cancelled_task__rejected(board):
    # arrange
    _failed(board)
    board.transition("A", Status.CANCELLED, "user")
    # act / assert
    with pytest.raises(ValueError, match="cancelled"):
        board.retry("A", "user")


def test_retry__project_agent__only_after_failed_turn(board):
    # arrange
    with pytest.raises(ValueError):
        board.retry("_project", "user")
    board.update_meta({"project_agent_state": "error", "project_turn_error": "упал CLI"})
    # act
    events = board.retry("_project", "user")
    # assert
    assert flat(events) == [wake("_project", "retry", error="упал CLI")]


def _started(board, key="A"):
    board.add_task(mk(key), "user")
    board.set_stage("work", "user")
    board.update_task(key, "system", {"session_id": "s-1", "worktree": "/tmp/wt"})


@pytest.mark.parametrize("actor", ["user", "project_agent"])
def test_request_rebase__by_user_or_project_agent__records_rebase_wake(board, actor):
    # arrange
    _started(board)
    # act
    events = board.request_rebase("A", actor)
    # assert
    assert flat(events) == [wake("A", "rebase", by=actor)]


def test_request_rebase__by_task_agent__forbidden(board):
    # arrange
    _started(board)
    # act / assert
    with pytest.raises(PermissionError, match="агент проекта"):
        board.request_rebase("A", "agent")


@pytest.mark.parametrize("status", ["approved", "done", "cancelled"])
def test_request_rebase__closed_task__rejected(board, status):
    # arrange
    _started(board)
    for to in ("in_review", status) if status != "cancelled" else (status,):
        board.transition("A", to, "user")
    # act / assert
    with pytest.raises(ValueError, match=status):
        board.request_rebase("A", "user")


def test_request_rebase__task_without_worktree__rejected(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.set_stage("work", "user")
    # act / assert
    with pytest.raises(ValueError, match="рабочей копии"):
        board.request_rebase("A", "user")


def test_add_task__no_key__next_key_assigned(tmp_home):
    # arrange
    store = ProjectStore("fix")
    store.create(ProjectConfig(slug="fix", title="Fix"))
    board = Board(store, clock=lambda: NOW)

    # act
    board.add_task(mk(""), "user")
    board.add_task(mk(""), "user")

    # assert
    assert sorted(store.load_tasks()) == ["FIX-1", "FIX-2"]


@pytest.mark.parametrize("keys, expected", [
    ([], "PAY-1"),
    (["T-1", "T-7", "X-3"], "T-8"),
    (["A-09", "A-10"], "A-11"),
    (["T-001"], "T-002"),
    (["B-1", "A-1"], "A-2"),
])
def test_next_task_key__existing_keys__most_common_prefix_next_number(board, keys, expected):
    # arrange
    for key in keys:
        board.add_task(mk(key), "user")

    # act, assert
    assert board.next_task_key() == expected


def free_board(repos=None):
    store = ProjectStore("fix")
    store.create(ProjectConfig(slug="fix", title="Fix", kind="free", stage="work", repos=list(repos or [])))
    return Board(store, clock=lambda: NOW)


def git_init(path, branch="main"):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True, capture_output=True)
    return path


def test_add_task__repo_path_in_free_project__repo_added_and_task_linked(tmp_home, tmp_path):
    # arrange
    repo = git_init(tmp_path / "code" / "api", branch="trunk")
    board = free_board()

    # act
    ev = board.add_task(mk(""), "user", repo_path=str(repo))

    # assert
    assert flat(ev)[:2] == [dict(type="config_changed", fields=["repos"]), dict(type="task_added", key="FIX-1")]
    assert board.store.load_config().repos == [{"name": "api", "path": str(repo.resolve()), "base": "trunk"}]
    assert board.store.load_tasks()["FIX-1"].repo == "api"


def test_add_task__same_repo_path_again__repo_not_duplicated(tmp_home, tmp_path, monkeypatch):
    # arrange: тот же репозиторий через ~, через симлинк, с / на конце и через подпапку
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = git_init(tmp_path / "code" / "api")
    (repo / "src").mkdir()
    (tmp_path / "link").symlink_to(repo)
    board = free_board()
    board.add_task(mk(""), "user", repo_path="~/code/api")

    # act
    for path in (str(tmp_path / "link"), str(repo) + "/", str(repo / "src")):
        ev = board.add_task(mk(""), "user", repo_path=path)
        assert "config_changed" not in [e.type for e in ev]

    # assert
    assert [r["name"] for r in board.store.load_config().repos] == ["api"]
    assert {t.repo for t in board.store.load_tasks().values()} == {"api"}


def test_add_task__repo_already_in_settings__its_name_used(tmp_home, tmp_path):
    repo = git_init(tmp_path / "api")
    board = free_board([{"name": "backend", "path": str(repo), "base": "main"}])
    board.add_task(mk(""), "user", repo_path=str(repo))
    assert board.store.load_tasks()["FIX-1"].repo == "backend"
    assert len(board.store.load_config().repos) == 1


def test_add_task__two_folders_with_same_name__second_numbered(tmp_home, tmp_path):
    # arrange
    first = git_init(tmp_path / "a" / "api")
    second = git_init(tmp_path / "b" / "api")
    board = free_board()

    # act
    board.add_task(mk(""), "user", repo_path=str(first))
    board.add_task(mk(""), "user", repo_path=str(second))

    # assert
    repos = board.store.load_config().repos
    assert [(r["name"], r["path"]) for r in repos] == [("api", str(first.resolve())), ("api-2", str(second.resolve()))]
    tasks = board.store.load_tasks()
    assert (tasks["FIX-1"].repo, tasks["FIX-2"].repo) == ("api", "api-2")


@pytest.mark.parametrize("make_path, text", [
    (lambda tmp: tmp / "missing", "Папка репозитория не найдена"),
    (lambda tmp: tmp / "plain", "Папка не является git-репозиторием"),
    (lambda tmp: "relative/api", "Путь к репозиторию должен быть абсолютным"),
])
def test_add_task__bad_repo_path__rejected_and_nothing_changed(tmp_home, tmp_path, make_path, text):
    # arrange
    (tmp_path / "plain").mkdir()
    board = free_board()

    # act
    with pytest.raises(ValueError, match=text):
        board.add_task(mk(""), "user", repo_path=str(make_path(tmp_path)))

    # assert
    assert board.store.load_tasks() == {}
    assert board.store.load_config().repos == []


def test_add_task__repo_path_in_plan_project__rejected(board, tmp_path):
    repo = git_init(tmp_path / "api")
    with pytest.raises(ValueError, match="только в проекте без плана"):
        board.add_task(mk("A"), "user", repo_path=str(repo))
    assert board.store.load_tasks() == {}


def test_add_task__repo_and_repo_path__rejected(tmp_home, tmp_path):
    repo = git_init(tmp_path / "api")
    with pytest.raises(ValueError, match="либо repo, либо repo_path"):
        free_board().add_task(mk("", repo="api"), "user", repo_path=str(repo))


def test_add_task__repo_path_with_bad_deps__repo_not_added(tmp_home, tmp_path):
    repo = git_init(tmp_path / "api")
    board = free_board()
    with pytest.raises(ValueError):
        board.add_task(mk("", deps=["NOPE-1"]), "user", repo_path=str(repo))
    assert board.store.load_config().repos == []


def test_proposal_add_task__repo_path__applied_with_repo(tmp_home, tmp_path):
    # arrange
    repo = git_init(tmp_path / "api")
    board = free_board()
    ev = board.propose("add_task", {"task": {"key": "FIX-1", "title": "t", "repo_path": str(repo)}}, "")
    pid = next(e.data["id"] for e in ev if e.type == "proposal_created")

    # act
    board.apply_proposal(pid)

    # assert
    assert board.store.load_tasks()["FIX-1"].repo == "api"
    assert [r["name"] for r in board.store.load_config().repos] == ["api"]


def test_proposal_add_task__repo_path_not_git__rejected(tmp_home, tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(ValueError, match="не является git-репозиторием"):
        free_board().propose("add_task", {"task": {"key": "FIX-1", "title": "t", "repo_path": str(tmp_path / "plain")}},
                             "")


# --- агент проекта создаёт задачи по прямой просьбе пользователя ---

def user_turn_board(turn_by_user=True, project_agent_tasks=True):
    store = ProjectStore("fix")
    store.create(ProjectConfig(slug="fix", title="Fix", kind="free", stage="work",
                               project_agent_tasks=project_agent_tasks))
    board = Board(store, clock=lambda: NOW)
    board.update_meta({"project_turn_by_user": turn_by_user})
    return board


def test_add_task__project_agent_in_user_turn__created_and_started(tmp_home):
    # arrange
    board = user_turn_board()

    # act
    ev = board.add_task(mk(""), "project_agent")

    # assert
    assert flat(ev) == [dict(type="task_added", key="FIX-1"), wake("FIX-1", "ready")]
    assert board.store.load_tasks()["FIX-1"].created_by == "project_agent"


def test_add_task__project_agent_outside_user_turn__forbidden_with_propose_hint(tmp_home):
    # arrange
    board = user_turn_board(turn_by_user=False)

    # act / assert
    with pytest.raises(PermissionError, match="propose add_task"):
        board.add_task(mk(""), "project_agent")
    assert board.store.load_tasks() == {}


def test_add_task__project_agent_tasks_disabled__forbidden_even_in_user_turn(tmp_home):
    # arrange
    board = user_turn_board(project_agent_tasks=False)

    # act / assert
    with pytest.raises(PermissionError, match="propose add_task"):
        board.add_task(mk(""), "project_agent")
    assert board.store.load_tasks() == {}


def test_add_task__by_user_or_applied_proposal__created_by_user(tmp_home):
    # arrange
    board = user_turn_board()
    ev = board.propose("add_task", {"task": {"key": "FIX-2", "title": "t"}}, "")
    pid = next(e.data["id"] for e in ev if e.type == "proposal_created")

    # act
    board.add_task(mk("FIX-1"), "user")
    board.apply_proposal(pid)

    # assert
    tasks = board.store.load_tasks()
    assert (tasks["FIX-1"].created_by, tasks["FIX-2"].created_by) == ("user", "user")
