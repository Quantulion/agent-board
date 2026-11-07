from __future__ import annotations

import pytest
from conftest import flat, make_task as mk, wake

from agent_board.model import Status
from agent_board.store import PROJECT_THREAD


def _task_payload(key, deps=()):
    return {"task": {"key": key, "title": key, "deps": list(deps)}}


def _texts(board, thread):
    return [m.text for m in board.store.read_thread(thread)]


def test_propose_does_not_change_graph_and_emits_created(board):
    board.add_task(mk("A"), "user")
    ev = board.propose("add_task", _task_payload("B", ["A"]), "нужна вторая задача")
    assert flat(ev) == [dict(type="proposal_created", id="p1")]
    assert set(board.store.load_tasks()) == {"A"}
    p = board.snapshot()["proposals"][0]
    assert (p["id"], p["kind"], p["status"], p["reason"]) == ("p1", "add_task", "open", "нужна вторая задача")


def test_propose_ids_are_sequential(board):
    board.add_task(mk("A"), "user")
    board.propose("cancel_task", {"key": "A"}, "")
    board.propose("set_deps", {"key": "A", "deps": []}, "")
    assert [p["id"] for p in board.snapshot()["proposals"]] == ["p1", "p2"]


def test_propose_unknown_kind_raises_value_error(board):
    with pytest.raises(ValueError):
        board.propose("drop_db", {}, "")


@pytest.mark.parametrize("kind, payload", [
    ("add_task", {}),
    ("add_task", {"task": "x"}),
    ("add_task", {"task": {"title": "no key"}}),
    ("set_deps", {}),
    ("set_deps", {"key": "X"}),
    ("set_deps", {"key": "X", "deps": "T-1"}),
    ("cancel_task", {}),
    ("cancel_task", {"key": 5}),
    ("add_task", None),
    ("set_deps", [1]),
])
def test_propose_malformed_payload_raises_value_error(board, kind, payload):
    with pytest.raises(ValueError):
        board.propose(kind, payload, "")
    assert board.snapshot()["proposals"] == []


@pytest.mark.parametrize("kind", ["set_deps", "cancel_task"])
def test_propose_unknown_task_raises_key_error(board, kind):
    with pytest.raises(KeyError):
        board.propose(kind, {"key": "X", "deps": []}, "")
    assert board.snapshot()["proposals"] == []


def test_propose_invalid_deps_raises_and_saves_nothing(board):
    with pytest.raises(ValueError):
        board.propose("add_task", _task_payload("B", ["NOPE"]), "")
    assert board.snapshot()["proposals"] == []


def test_apply_add_task_creates_task_wakes_agent_and_posts_system_messages(board):
    board.propose("add_task", _task_payload("B"), "r")
    ev = board.apply_proposal("p1")
    assert flat(ev)[:3] == [
        dict(type="task_added", key="B"),
        wake("B", "ready"),
        dict(type="proposal_applied", id="p1"),
    ]
    assert "B" in board.store.load_tasks()
    assert board.snapshot()["proposals"][0]["status"] == "applied"
    assert any("Применено предложение p1" in t for t in _texts(board, PROJECT_THREAD))
    assert any("Применено предложение p1" in t for t in _texts(board, "B"))
    assert {m.author for m in board.store.read_thread("B")} == {"system"}


def test_apply_set_deps_updates_deps_and_notifies_task_thread(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.propose("set_deps", {"key": "B", "deps": ["A"]}, "")
    board.apply_proposal("p1")
    assert board.store.load_tasks()["B"].deps == ["A"]
    assert _texts(board, "B") and _texts(board, PROJECT_THREAD)
    assert _texts(board, "A") == []


def test_apply_cancel_task_cancels_via_user(board):
    board.add_task(mk("A"), "user")
    board.propose("cancel_task", {"key": "A"}, "")
    ev = board.apply_proposal("p1")
    assert board.store.load_tasks()["A"].status == Status.CANCELLED
    assert dict(type="status_changed", key="A", **{"from": "not_started", "to": "cancelled", "actor": "user"}) in flat(ev)
    assert _texts(board, "A")


def test_apply_stale_proposal_raises_and_stays_open(board):
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.propose("set_deps", {"key": "A", "deps": ["B"]}, "")
    board.set_deps("B", ["A"], "user")  # чужая правка создаёт цикл
    seq = board.snapshot()["last_seq"]
    with pytest.raises(ValueError):
        board.apply_proposal("p1")
    snap = board.snapshot()
    assert snap["proposals"][0]["status"] == "open"
    assert snap["last_seq"] == seq
    assert board.store.load_tasks()["A"].deps == []


def test_apply_twice_raises_value_error(board):
    board.propose("add_task", _task_payload("B"), "")
    board.apply_proposal("p1")
    with pytest.raises(ValueError):
        board.apply_proposal("p1")


def test_apply_unknown_id_raises_key_error(board):
    with pytest.raises(KeyError):
        board.apply_proposal("p9")
    with pytest.raises(KeyError):
        board.reject_proposal("p9")


def test_reject_marks_rejected_and_wakes_project_agent(board):
    board.propose("add_task", _task_payload("B"), "")
    ev = board.reject_proposal("p1")
    assert flat(ev) == [dict(type="proposal_rejected", id="p1"), wake("_project", "proposal_rejected")]
    assert board.snapshot()["proposals"][0]["status"] == "rejected"
    assert "B" not in board.store.load_tasks()


def test_reject_non_open_raises_value_error(board):
    board.propose("add_task", _task_payload("B"), "")
    board.reject_proposal("p1")
    with pytest.raises(ValueError):
        board.reject_proposal("p1")
    with pytest.raises(ValueError):
        board.apply_proposal("p1")


@pytest.mark.parametrize("task", [
    {"key": "B", "title": "b", "status": "done"},
    {"key": "B", "title": "b", "session_id": "s"},
    {"key": "B", "title": "b", "questions": []},
    {"key": "B", "title": "b", "deps": "A"},
    {"key": "B", "title": "b", "deps": [1]},
    {"key": "B", "title": "b", "estimate": "3"},
    {"key": "B", "title": "b", "estimate": True},
    {"key": "B", "title": "b", "kind": "deploy"},
    {"key": "B", "title": 5},
    {"key": "B"},
    {"key": "_B", "title": "b"},
    {"key": "a", "title": "dup"},
])
def test_propose_add_task_rejects_non_creatable_fields(board, task):
    # arrange
    board.add_task(mk("A"), "user")

    # act / assert
    with pytest.raises(ValueError):
        board.propose("add_task", {"task": task}, "")
    assert board.snapshot()["proposals"] == []


def test_propose_add_task_accepts_all_creatable_fields(board):
    # arrange
    board.add_task(mk("A"), "user")
    task = {"key": "B", "title": "b", "description": "d", "done_when": "w", "repo": "r",
            "kind": "research", "estimate": 2, "deps": ["A"]}

    # act
    board.propose("add_task", {"task": task}, "")
    board.apply_proposal("p1")

    # assert
    b = board.store.load_tasks()["B"]
    assert (b.kind, b.estimate, b.deps, b.status) == ("research", 2, ["A"], Status.NOT_STARTED)


def _proposal(board, pid):
    return next(p for p in board.snapshot()["proposals"] if p["id"] == pid)


def test_apply_add_task_marks_duplicate_key_proposal_obsolete(board):
    # arrange
    board.propose("add_task", _task_payload("B"), "первая версия")
    board.propose("add_task", _task_payload("B"), "исправленная версия")

    # act
    ev = board.apply_proposal("p2")

    # assert
    assert dict(type="proposal_obsolete", id="p1", reason="exists") in flat(ev)
    assert (_proposal(board, "p1")["status"], _proposal(board, "p1")["obsolete_reason"]) == ("obsolete", "exists")
    assert _proposal(board, "p2")["status"] == "applied"
    with pytest.raises(ValueError):
        board.apply_proposal("p1")


def test_add_task_by_user_marks_proposal_with_same_key_obsolete_case_insensitive(board):
    # arrange
    board.propose("add_task", _task_payload("b"), "")

    # act
    board.add_task(mk("B"), "user")

    # assert
    assert _proposal(board, "p1")["status"] == "obsolete"


def test_cancel_marks_set_deps_and_cancel_proposals_for_task_obsolete(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.add_task(mk("B"), "user")
    board.propose("set_deps", {"key": "A", "deps": ["B"]}, "")
    board.propose("cancel_task", {"key": "A"}, "")
    board.propose("cancel_task", {"key": "B"}, "")

    # act
    ev = board.transition("A", Status.CANCELLED, "user")

    # assert
    assert [e for e in flat(ev) if e["type"] == "proposal_obsolete"] == [
        dict(type="proposal_obsolete", id="p1", reason="cancelled"),
        dict(type="proposal_obsolete", id="p2", reason="cancelled"),
    ]
    assert _proposal(board, "p3")["status"] == "open"


def test_apply_cancel_task_marks_other_proposals_for_task_obsolete(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.propose("cancel_task", {"key": "A"}, "")
    board.propose("set_deps", {"key": "A", "deps": []}, "")

    # act
    board.apply_proposal("p1")

    # assert
    assert _proposal(board, "p1")["status"] == "applied"
    assert (_proposal(board, "p2")["status"], _proposal(board, "p2")["obsolete_reason"]) == ("obsolete", "cancelled")


def test_proposal_for_deleted_task_becomes_obsolete_on_next_graph_change(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.propose("set_deps", {"key": "A", "deps": []}, "")
    (board.store.root / "tasks" / "A.json").unlink()

    # act
    board.add_task(mk("C"), "user")

    # assert
    assert (_proposal(board, "p1")["status"], _proposal(board, "p1")["obsolete_reason"]) == ("obsolete", "missing")


def test_failed_apply_reopens_proposal(board, monkeypatch):
    # arrange
    board.propose("add_task", _task_payload("B"), "")

    def boom(*args, **kwargs):
        raise RuntimeError("сбой записи")

    monkeypatch.setattr(board, "add_task", boom)

    # act
    with pytest.raises(RuntimeError):
        board.apply_proposal("p1")

    # assert
    assert _proposal(board, "p1")["status"] == "open"


# --- действия пользователя: set_config, set_status, ship, stage, retry ---

def _ship_ready(board, *keys):
    board.update_config({"ship": {"command": "git push {branch}", "timeout": 600}})
    for key in keys:
        board.add_task(mk(key), "user")
        board.transition(key, Status.IN_REVIEW, "agent")
        board.approve(key, "user")


def _failed_turn(board, key):
    board.set_stage("work", "user")
    board.add_task(mk(key), "user")
    board.update_task(key, "system", {"agent_state": "error", "turn_error": "boom"})


def test_set_config__valid_fields__proposed_without_change_and_applied_by_user(board):
    # arrange
    board.propose("set_config", {"fields": {"max_agents": 2}}, "больше параллельности")
    assert board.store.load_config().max_agents != 2

    # act
    ev = board.apply_proposal("p1")

    # assert
    assert board.store.load_config().max_agents == 2
    assert dict(type="config_changed", fields=["max_agents"]) in flat(ev)
    assert any("max_agents" in t for t in _texts(board, PROJECT_THREAD))


@pytest.mark.parametrize("payload", [
    {}, {"fields": {}}, {"fields": "x"}, {"fields": {"stage": "work"}}, {"fields": {"nope": 1}},
    {"fields": {"max_agents": 0}}, {"fields": {"branch_template": "no-key"}},
])
def test_set_config__invalid_fields__rejected_on_propose(board, payload):
    with pytest.raises(ValueError):
        board.propose("set_config", payload, "")
    assert board.snapshot()["proposals"] == []


def test_set_config__same_value__rejected_on_propose(board):
    current = board.store.load_config().max_agents
    with pytest.raises(ValueError, match="ничего не меняет"):
        board.propose("set_config", {"fields": {"max_agents": current}}, "")


def test_set_config__field_changed_after_propose__stale(board):
    # arrange
    board.propose("set_config", {"fields": {"max_agents": 2}}, "")
    board.set_config("user", {"max_agents": 5})

    # act / assert
    with pytest.raises(ValueError, match="устарело.*max_agents"):
        board.apply_proposal("p1")
    assert board.store.load_config().max_agents == 5
    assert board.snapshot()["proposals"][0]["status"] == "open"


def test_set_config__invalid_at_apply__rejected_with_save_checks(board, monkeypatch):
    # arrange
    board.propose("set_config", {"fields": {"max_agents": 2}}, "")
    import agent_board.board as board_module

    def broken(config, changed, repos):
        raise ValueError("проверка сохранения не прошла")
    monkeypatch.setattr(board_module, "validate_fields", broken)

    # act / assert
    with pytest.raises(ValueError, match="проверка сохранения"):
        board.apply_proposal("p1")
    assert board.store.load_config().max_agents != 2


@pytest.mark.parametrize("to", ["done", "cancelled", "in_progress"])
def test_set_status__user_transition__applied_as_user(board, to):
    # arrange
    board.add_task(mk("A"), "user")
    board.propose("set_status", {"key": "A", "to": to}, "")

    # act
    ev = board.apply_proposal("p1")

    # assert
    assert board.store.load_tasks()["A"].status == Status(to)
    assert dict(type="status_changed", key="A", actor="user", **{"from": "not_started", "to": to}) in flat(ev)
    assert _texts(board, "A")


def test_set_status__approved__goes_through_approve(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_REVIEW, "agent")
    board.propose("set_status", {"key": "A", "to": "approved", "from": "in_review"}, "")

    # act
    board.apply_proposal("p1")

    # assert
    assert board.store.load_tasks()["A"].status == Status.APPROVED


@pytest.mark.parametrize("payload, error", [
    ({"key": "A", "to": "approved"}, ValueError),
    ({"key": "A", "to": "blocked"}, ValueError),
    ({"key": "A", "to": "not_started"}, ValueError),
    ({"key": "A", "to": "nope"}, ValueError),
    ({"key": "A"}, ValueError),
    ({"key": "A", "to": "done", "from": "in_review"}, ValueError),
    ({"key": "X", "to": "done"}, KeyError),
])
def test_set_status__not_allowed_now__rejected_on_propose(board, payload, error):
    board.add_task(mk("A"), "user")
    with pytest.raises(error):
        board.propose("set_status", payload, "")
    assert board.snapshot()["proposals"] == []


def test_set_status__status_changed_after_propose__stale(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.IN_PROGRESS, "agent")
    board.propose("set_status", {"key": "A", "to": "done"}, "")
    board.transition("A", Status.IN_REVIEW, "agent")
    seq = board.snapshot()["last_seq"]

    # act / assert
    with pytest.raises(ValueError, match="устарело.*in_progress.*in_review"):
        board.apply_proposal("p1")
    assert board.store.load_tasks()["A"].status == Status.IN_REVIEW
    assert board.snapshot()["last_seq"] == seq


def test_ship__approved_tasks__applied_as_ship_request(board):
    # arrange
    _ship_ready(board, "A", "B")
    board.propose("ship", {"keys": ["B", "A"]}, "")

    # act
    ev = board.apply_proposal("p1")

    # assert
    assert dict(type="ship_requested", keys=["A", "B"]) in flat(ev)
    assert board.store.load_tasks()["A"].ship["state"] == "queued"
    assert _texts(board, "A") and _texts(board, "B")


@pytest.mark.parametrize("payload", [{}, {"keys": []}, {"keys": "A"}, {"keys": ["A", "A"]}, {"keys": ["C"]}])
def test_ship__invalid_keys__rejected_on_propose(board, payload):
    _ship_ready(board, "A")
    board.add_task(mk("C"), "user")
    with pytest.raises(ValueError):
        board.propose("ship", payload, "")


def test_ship__task_returned_to_work_after_propose__rejected(board):
    # arrange
    _ship_ready(board, "A")
    board.propose("ship", {"keys": ["A"]}, "")
    board.transition("A", Status.IN_PROGRESS, "user")

    # act / assert
    with pytest.raises(ValueError, match="одобренную"):
        board.apply_proposal("p1")
    assert board.snapshot()["proposals"][0]["status"] == "open"


def test_stage__next_stage__applied_by_user(board):
    # arrange
    board.propose("stage", {"stage": "work"}, "")

    # act
    ev = board.apply_proposal("p1")

    # assert
    assert dict(type="stage_changed", **{"from": "decomposition", "to": "work"}) in flat(ev)
    assert board.store.load_config().stage == "work"


@pytest.mark.parametrize("payload", [{}, {"stage": "done"}, {"stage": "decomposition"}, {"stage": 1}])
def test_stage__not_allowed__rejected_on_propose(board, payload):
    with pytest.raises(ValueError):
        board.propose("stage", payload, "")


def test_stage__stage_changed_after_propose__stale(board):
    # arrange
    board.propose("stage", {"stage": "work"}, "")
    board.set_stage("tracker", "user")

    # act / assert
    with pytest.raises(ValueError, match="устарело.*decomposition.*tracker"):
        board.apply_proposal("p1")
    assert board.store.load_config().stage == "tracker"


def test_retry__failed_turn__applied_as_user_retry(board):
    # arrange
    _failed_turn(board, "A")
    board.propose("retry", {"key": "A"}, "")

    # act
    ev = board.apply_proposal("p1")

    # assert
    assert wake("A", "retry", error="boom") in flat(ev)


def test_retry__agent_not_failed__rejected_on_propose(board):
    board.set_stage("work", "user")
    board.add_task(mk("A"), "user")
    with pytest.raises(ValueError):
        board.propose("retry", {"key": "A"}, "")


def test_retry__agent_recovered_after_propose__rejected(board):
    # arrange
    _failed_turn(board, "A")
    board.propose("retry", {"key": "A"}, "")
    board.update_task("A", "system", {"agent_state": "idle"})

    # act / assert
    with pytest.raises(ValueError, match="упавший ход"):
        board.apply_proposal("p1")


def test_project_agent__cannot_do_proposed_actions_directly(board):
    # arrange
    _ship_ready(board, "A")
    board.add_task(mk("B"), "user")

    # act / assert
    with pytest.raises(PermissionError):
        board.set_config("project_agent", {"max_agents": 2})
    with pytest.raises(PermissionError):
        board.transition("B", Status.DONE, "project_agent")
    with pytest.raises(PermissionError):
        board.approve("B", "project_agent")
    with pytest.raises(PermissionError):
        board.request_ship(["A"], "project_agent")
    with pytest.raises(PermissionError):
        board.set_stage("work", "project_agent")
    with pytest.raises(PermissionError):
        board.retry("B", "project_agent")


def test_cancel__set_status_and_ship_proposals_for_task__obsolete(board):
    # arrange
    _ship_ready(board, "A")
    board.add_task(mk("B"), "user")
    board.propose("set_status", {"key": "B", "to": "done"}, "")
    board.propose("set_config", {"fields": {"max_agents": 2}}, "")

    # act
    board.transition("B", Status.CANCELLED, "user")

    # assert
    assert (_proposal(board, "p1")["status"], _proposal(board, "p1")["obsolete_reason"]) == ("obsolete", "cancelled")
    assert _proposal(board, "p2")["status"] == "open"


def test_set_status__from_cancelled__not_obsolete_on_graph_change(board):
    # arrange
    board.add_task(mk("A"), "user")
    board.transition("A", Status.CANCELLED, "user")
    board.propose("set_status", {"key": "A", "to": "not_started"}, "")

    # act
    board.add_task(mk("C"), "user")
    board.apply_proposal("p1")

    # assert
    assert board.store.load_tasks()["A"].status == Status.NOT_STARTED
