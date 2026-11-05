from __future__ import annotations

import pytest
from conftest import make_task as mk

from agent_board.graph import (
    base_for,
    critical_path,
    dependents,
    display_status,
    find_cycle,
    open_deps,
    ready_tasks,
    validate_deps,
)
from agent_board.model import Status


def test_dep_satisfied_by_approved_only_with_chains():
    t = {"A": mk("A", status=Status.APPROVED), "B": mk("B", deps=["A"])}
    assert open_deps(t["B"], t, chains=True) == []
    assert open_deps(t["B"], t, chains=False) == ["A"]


def test_dep_done_satisfied_without_chains():
    t = {"A": mk("A", status=Status.DONE), "B": mk("B", deps=["A"])}
    assert open_deps(t["B"], t, chains=False) == []


def test_unknown_dep_counts_as_open():
    t = {"B": mk("B", deps=["X"])}
    assert open_deps(t["B"], t, chains=True) == ["X"]


def test_display_status_blocked_by_open_dep():
    t = {"A": mk("A"), "B": mk("B", deps=["A"])}
    assert display_status(t["B"], t, chains=True) == Status.BLOCKED
    assert display_status(t["A"], t, chains=True) == Status.NOT_STARTED


def test_display_status_blocked_by_external_blocker():
    t = {"A": mk("A", external_blocker="ждём доступ")}
    assert display_status(t["A"], t, chains=True) == Status.BLOCKED


def test_display_status_keeps_stored_status_when_started():
    t = {"A": mk("A"), "B": mk("B", deps=["A"], status=Status.IN_PROGRESS)}
    assert display_status(t["B"], t, chains=True) == Status.IN_PROGRESS


def test_cancelled_dep_does_not_block():
    t = {"A": mk("A", status=Status.CANCELLED), "B": mk("B", deps=["A"])}
    assert open_deps(t["B"], t, chains=True) == []
    assert display_status(t["B"], t, chains=True) == Status.NOT_STARTED


def test_ready_tasks_sorted_and_exclude_blocked():
    t = {
        "C": mk("C"),
        "A": mk("A"),
        "B": mk("B", deps=["C"]),
        "D": mk("D", external_blocker="x"),
        "E": mk("E", status=Status.IN_PROGRESS),
    }
    assert ready_tasks(t, chains=True) == ["A", "C"]


def test_find_cycle_returns_path():
    t = {"A": mk("A", deps=["B"]), "B": mk("B", deps=["C"]), "C": mk("C", deps=["A"])}
    cycle = find_cycle(t)
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert sorted(cycle[:-1]) == ["A", "B", "C"]
    # каждое ребро пути - зависимость
    for a, b in zip(cycle, cycle[1:]):
        assert b in t[a].deps


def test_find_cycle_none_for_dag():
    t = {"A": mk("A"), "B": mk("B", deps=["A"]), "C": mk("C", deps=["A", "B"])}
    assert find_cycle(t) is None


def test_validate_deps_rejects_unknown_self_and_cycle():
    t = {"A": mk("A"), "B": mk("B", deps=["A"])}
    with pytest.raises(ValueError, match="неизвестн"):
        validate_deps("B", ["X"], t)
    with pytest.raises(ValueError, match="себя"):
        validate_deps("B", ["B"], t)
    with pytest.raises(ValueError, match="цикл"):
        validate_deps("A", ["B"], t)


def test_validate_deps_accepts_new_key_and_valid_deps():
    t = {"A": mk("A")}
    validate_deps("N", ["A"], t)
    validate_deps("A", [], t)


def test_base_for_single_approved_dep_uses_its_branch():
    t = {"A": mk("A", status=Status.APPROVED, branch="pay-101"), "B": mk("B", deps=["A"])}
    assert base_for(t["B"], t, True, "main") == ("pay-101", [])


def test_base_for_two_unmerged_deps_merges_both():
    t = {
        "A": mk("A", status=Status.APPROVED, branch="pay-101"),
        "C": mk("C", status=Status.SENT, branch="pay-102"),
        "B": mk("B", deps=["A", "C"]),
    }
    assert base_for(t["B"], t, True, "main") == ("main", ["pay-101", "pay-102"])


def test_base_for_done_deps_uses_default():
    t = {"A": mk("A", status=Status.DONE, branch="pay-101"), "B": mk("B", deps=["A"])}
    assert base_for(t["B"], t, True, "main") == ("main", [])


def test_base_for_without_chains_uses_default():
    t = {"A": mk("A", status=Status.APPROVED, branch="pay-101"), "B": mk("B", deps=["A"])}
    assert base_for(t["B"], t, False, "main") == ("main", [])


def test_critical_path_by_estimates():
    t = {
        "A": mk("A", estimate=1),
        "B": mk("B", deps=["A"], estimate=5),
        "C": mk("C", deps=["A"], estimate=1),
        "D": mk("D", deps=["B", "C"], estimate=2),
    }
    assert critical_path(t) == ["A", "B", "D"]


def test_critical_path_defaults_estimate_to_one_and_skips_cancelled():
    t = {
        "A": mk("A"),
        "X": mk("X", deps=["A"], estimate=100, status=Status.CANCELLED),
        "B": mk("B", deps=["A"]),
    }
    assert critical_path(t) == ["A", "B"]


def test_critical_path_empty():
    assert critical_path({}) == []


def test_dependents_returns_direct_only_sorted():
    t = {
        "A": mk("A"),
        "C": mk("C", deps=["A"]),
        "B": mk("B", deps=["A"]),
        "D": mk("D", deps=["B"]),
    }
    assert dependents("A", t) == ["B", "C"]
