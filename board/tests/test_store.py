from __future__ import annotations

import json
import os

import pytest
from conftest import make_task

from agent_board import store as store_mod
from agent_board.config import ProjectConfig
from agent_board.model import Message, Proposal
from agent_board.store import ProjectStore, atomic_write_json, board_home, list_projects


def new_store(slug="pay"):
    s = ProjectStore(slug)
    s.create(ProjectConfig(slug=slug, title="Платежи"))
    return s


def test_create_and_reload_project(tmp_home):
    s = ProjectStore("pay")
    s.create(ProjectConfig(slug="pay", title="Платежи"))
    assert list_projects() == ["pay"]
    assert s.load_config().title == "Платежи"


def test_board_home_uses_env(tmp_home):
    assert board_home() == tmp_home


def test_create_existing_raises(tmp_home):
    new_store()
    with pytest.raises(FileExistsError):
        ProjectStore("pay").create(ProjectConfig(slug="pay", title="X"))


def test_list_projects_empty(tmp_home):
    assert list_projects() == []


def test_save_and_load_task(tmp_home):
    s = new_store()
    t = make_task("PAY-101")
    s.save_task(t)
    assert s.load_tasks()["PAY-101"] == t


def test_events_have_increasing_seq_and_filter_since(tmp_home):
    s = new_store()
    e1 = s.append_event("a", {})
    e2 = s.append_event("b", {"x": 1})
    assert (e1.seq, e2.seq) == (1, 2)
    assert [e.type for e in s.read_events(since=1)] == ["b"]


def test_seq_continues_after_reopen(tmp_home):
    s = new_store()
    s.append_event("a", {})
    s.append_event("b", {})
    assert ProjectStore("pay").append_event("c", {}).seq == 3


def test_broken_tail_line_is_skipped(tmp_home):
    s = new_store()
    s.append_event("a", {})
    with open(s.root / "events.jsonl", "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "ty')
    assert [e.type for e in s.read_events()] == ["a"]


def test_thread_append_and_read_order(tmp_home):
    s = new_store()
    s.append_message("PAY-101", Message(id="m1", author="user", text="раз", at="t"))
    s.append_message("PAY-101", Message(id="m2", author="agent", text="два", at="t"))
    s.append_message("_project", Message(id="m3", author="user", text="проект", at="t"))
    assert [m.id for m in s.read_thread("PAY-101")] == ["m1", "m2"]
    assert [m.id for m in s.read_thread("_project")] == ["m3"]
    assert s.read_thread("PAY-999") == []


def test_proposals_roundtrip(tmp_home):
    s = new_store()
    assert s.load_proposals() == []
    ps = [Proposal(id="p1", kind="new_task", payload={"title": "T"}, reason="r")]
    s.save_proposals(ps)
    assert s.load_proposals() == ps


def test_meta_defaults_and_roundtrip(tmp_home):
    s = new_store()
    assert s.load_meta() == {"project_unread": 0}
    s.save_meta({"project_unread": 2})
    assert ProjectStore("pay").load_meta() == {"project_unread": 2}


def test_atomic_write_leaves_old_file_on_failure(tmp_home, monkeypatch):
    path = tmp_home / "f.json"
    atomic_write_json(path, {"v": 1})
    real_dump = json.dump

    def flaky(*a, **kw):
        raise OSError("boom")

    monkeypatch.setattr(store_mod.json, "dump", flaky)
    with pytest.raises(OSError):
        atomic_write_json(path, {"v": 2})
    monkeypatch.setattr(store_mod.json, "dump", real_dump)
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert os.listdir(tmp_home) == ["f.json"]


def test_append_after_truncated_tail_keeps_new_records(tmp_home):
    s = new_store()
    s.append_event("a", {})
    s.append_message("PAY-101", Message(id="m1", author="user", text="x", at="t"))
    with open(s.root / "events.jsonl", "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "ty')
    with open(s.root / "threads" / "PAY-101.jsonl", "a", encoding="utf-8") as f:
        f.write('{"id": "m2", "au')
    e = ProjectStore("pay").append_event("b", {})
    s.append_message("PAY-101", Message(id="m3", author="user", text="y", at="t"))
    assert e.seq == 2
    assert [x.type for x in s.read_events()] == ["a", "b"]
    assert [m.id for m in s.read_thread("PAY-101")] == ["m1", "m3"]


def test_invalid_identifiers_raise(tmp_home):
    with pytest.raises(ValueError):
        ProjectStore("../x")
    s = new_store()
    with pytest.raises(ValueError):
        s.append_message("a/b", Message(id="m", author="u", text="t", at="t"))
    with pytest.raises(ValueError):
        s.read_thread("a/b")
    with pytest.raises(ValueError):
        s.save_task(make_task(".."))
    s.append_message("_project", Message(id="m", author="u", text="t", at="t"))


def test_meta_merges_over_defaults(tmp_home):
    s = new_store()
    s.save_meta({"other": 1})
    assert s.load_meta() == {"project_unread": 0, "other": 1}
