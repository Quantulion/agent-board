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
