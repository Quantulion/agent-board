from __future__ import annotations

import json
import threading
import time

import pytest

from agent_board import tracker
from agent_board.config import ProjectConfig
from agent_board.model import Status
from agent_board.server import BoardServer
from agent_board.store import ProjectStore
from agent_board.tracker import TrackerSync

from conftest import make_task
from fakes.fake_github import FakeGitHub
from fakes.fake_jira import FakeJira

TRACKER_TOKEN = "trk-S3CRET-value-42"
JIRA_MAP = {"Open": None, "In Progress": "in_progress", "Reopened": "in_progress", "Done": "done"}


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIRA_TOKEN", TRACKER_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TRACKER_TOKEN)


@pytest.fixture
def app(tmp_home):
    """Незапущенный сервер: синхронизации нужны только борды проектов, HTTP не поднимается."""
    ProjectStore("pay").create(ProjectConfig(slug="pay", title="Платежи", chains=True))
    return BoardServer(port=0)


@pytest.fixture
def jira():
    fake = FakeJira(authorization="Bearer " + TRACKER_TOKEN).start()
    yield fake
    fake.stop()


@pytest.fixture
def github():
    fake = FakeGitHub(token=TRACKER_TOKEN).start()
    yield fake
    fake.stop()


def add_task(app, key, **extra):
    app.board_for("pay").add_task(make_task(key, **extra), "user")


def create_issues(app):
    result = tracker.create_issues(app.board_for("pay"), "user")
    assert result["errors"] == []


def with_jira(app, jira, stage="tracker"):
    board = app.board_for("pay")
    board.update_config({"tracker": {"type": "jira", "url": jira.url, "project": "PROJ", "issue_type": "Task",
                                     "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}})
    add_task(app, "A")
    add_task(app, "B", deps=["A"])
    if stage == "tracker":
        board.set_stage("tracker", "user")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def sync_project(app, jira, clock, statuses=None, poll=30, stage="work"):
    """Проект с задачами A, B(A), заведёнными в поддельной Jira; синхронизация с поддельными часами."""
    with_jira(app, jira)
    create_issues(app)
    board = app.board_for("pay")
    tracker_cfg = dict(board.store.load_config().tracker, statuses=JIRA_MAP if statuses is None else statuses,
                       poll_seconds=poll)
    board.update_config({"tracker": tracker_cfg})
    if stage != "tracker":
        board.set_stage("work", "user")
        if stage == "done":
            board.set_stage("done", "user")
    app.tracker_sync = TrackerSync(app, clock=clock)
    return app.tracker_sync


def sync_state(app):
    return app.board_for("pay").snapshot()["tracker_sync"]


def task_status(app, key):
    return next(t["status"] for t in app.board_for("pay").snapshot()["tasks"] if t["key"] == key)


def events_of(app, type):
    return [e.to_dict() for e in app.board_for("pay").store.read_events() if e.type == type]


def test_sync__tracker_done__marks_done_and_unblocks_dependent(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "done"
    changes = [e["data"] for e in events_of(app, "status_changed") if e["data"]["actor"] == "tracker"]
    assert changes == [{"key": "A", "from": "not_started", "to": "done", "actor": "tracker"}]
    assert [e["data"]["key"] for e in events_of(app, "task_unblocked")] == ["B"]
    assert sync_state(app)["changed"] == ["A"]
    assert events_of(app, "tracker_synced")[-1]["data"] == {"ok": True, "changed": ["A"], "error": None}


def test_sync__reopened_in_tracker__wakes_agent(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    sync.tick()
    jira.statuses["PROJ-1"] = "Reopened"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "in_progress"
    wakes = [e["data"] for e in events_of(app, "agent_wake") if e["data"].get("reason") == "returned"]
    assert wakes and wakes[-1]["key"] == "A" and wakes[-1]["from"] == "done"


def test_sync__default_interval_30_seconds__is_respected(app, jira, clock):
    # arrange
    with_jira(app, jira)
    create_issues(app)
    app.board_for("pay").set_stage("work", "user")
    sync = app.tracker_sync = TrackerSync(app, clock=clock)
    sync.tick()
    after_first = len(jira.requests)
    # act
    clock.now = 29
    sync.tick()
    unchanged = len(jira.requests)
    clock.now = 30
    sync.tick()
    # assert
    assert after_first > 0 and unchanged == after_first and len(jira.requests) > after_first
    assert sync.next_due("pay") == 60


def test_sync__not_work_stage__no_requests_and_no_worker_threads(app, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(app, jira, clock, stage="tracker")
    before = len(jira.requests)
    started = []
    real_worker = sync._worker
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: (started.append(slug), real_worker(slug, lock)))
    # act
    sync.tick()
    # assert
    assert len(jira.requests) == before and sync_state(app) is None and started == []


def test_sync__done_stage__still_syncs(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, stage="done")
    before = len(jira.requests)
    # act
    sync.tick()
    # assert
    assert len(jira.requests) > before and sync_state(app)["ok"] is True


def test_sync__unchanged_ticks__emit_no_events(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    count = len(events_of(app, "tracker_synced"))
    # act
    for _ in range(3):
        clock.now += 30
        sync.tick()
    # assert
    assert count == 1 and len(events_of(app, "tracker_synced")) == 1


def test_sync__mapping_is_case_insensitive(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, statuses={"in progress": "in_progress", "DONE": "done"})
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "IN PROGRESS"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "done" and task_status(app, "B") == "in_progress"
    assert sync_state(app)["unknown_statuses"] == []


def test_sync__empty_mapping__only_reads_and_lists_unknown_statuses(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, statuses={})
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "not_started"
    assert sync_state(app)["unknown_statuses"] == ["Done", "Open"] and sync_state(app)["changed"] == []
    assert events_of(app, "status_changed") == [] or all(
        e["data"]["actor"] != "tracker" for e in events_of(app, "status_changed"))


def test_sync__unmapped_status__is_listed_and_ignored(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Code Review"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "not_started" and sync_state(app)["unknown_statuses"] == ["Code Review"]


def test_sync__board_status_not_reverted_while_tracker_unchanged(app, jira, clock):
    # arrange: в трекере «In Progress», а доска ушла дальше — это не повод возвращать задачу
    sync = sync_project(app, jira, clock)
    jira.statuses["PROJ-1"] = "In Progress"
    sync.tick()
    board = app.board_for("pay")
    board.transition("A", Status.IN_REVIEW, "agent")
    # act
    clock.now += 30
    sync.tick()
    # assert
    assert task_status(app, "A") == "in_review"


def test_sync__task_transition_error__does_not_stop_others(app, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(app, jira, clock, statuses={"Open": None, "Done": "done"})
    sync.tick()
    board = app.board_for("pay")
    real = board.transition

    def flaky(key, to, actor, *a, **kw):
        if key == "A":
            raise ValueError("нельзя")
        return real(key, to, actor, *a, **kw)

    monkeypatch.setattr(board, "transition", flaky)
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(app)
    assert task_status(app, "B") == "done" and task_status(app, "A") == "not_started"
    assert state["changed"] == ["B"] and [e["key"] for e in state["errors"]] == ["A"]


def test_sync__401__pauses_without_further_requests(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    jira.fail_all = (401, None)
    # act
    sync.tick()
    count = len(jira.requests)
    for _ in range(3):
        clock.now += 30
        sync.tick()
    # assert
    state = sync_state(app)
    assert len(jira.requests) == count and state["paused"] is True and state["ok"] is False
    assert "JIRA_TOKEN" in state["error"]


def test_sync__manual_retry__runs_now_and_unpauses_on_success(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    jira.fail_all = (401, None)
    sync.tick()
    board = app.board_for("pay")
    # act
    with pytest.raises(PermissionError):
        sync.sync_now(board, "agent")
    still = sync.sync_now(board, "user")
    jira.fail_all = None
    fixed = sync.sync_now(board, "user")
    # assert
    assert still["paused"] is True and still["ok"] is False
    assert fixed["ok"] is True and fixed["paused"] is False
    assert sync_state(app)["paused"] is False
    clock.now += 30
    before = len(jira.requests)
    sync.tick()
    assert len(jira.requests) > before


def test_sync__manual_in_wrong_stage__is_rejected(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, stage="tracker")
    # act / assert
    with pytest.raises(ValueError, match="work"):
        sync.sync_now(app.board_for("pay"), "user")


def test_sync__tracker_config_change__unpauses(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    jira.fail_all = (401, None)
    sync.tick()
    jira.fail_all = None
    board = app.board_for("pay")
    # act
    board.update_config({"tracker": dict(board.store.load_config().tracker, labels=["x"])})
    sync.tick()
    # assert
    assert sync_state(app)["paused"] is False and sync_state(app)["ok"] is True


def test_sync__paused_state_survives_restart_until_config_changes(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    jira.fail_all = (403, None)
    sync.tick()
    count = len(jira.requests)
    # act: новый экземпляр (рестарт сервера) читает паузу из проекта
    app.tracker_sync = TrackerSync(app, clock=clock)
    app.tracker_sync.tick()
    # assert
    assert len(jira.requests) == count and sync_state(app)["paused"] is True


def test_sync__server_errors__back_off_doubling_up_to_5_minutes(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, poll=30)
    jira.fail_all = (503, {"errorMessages": ["down"]})
    delays = []
    # act
    for _ in range(7):
        sync.tick()
        delays.append(sync.next_due("pay") - clock.now)
        clock.now = sync.next_due("pay")
    # assert
    assert delays == [30, 60, 120, 240, 300, 300, 300]
    assert sync_state(app)["paused"] is False and sync_state(app)["ok"] is False


def test_sync__recovery_after_network_error__resets_interval(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, poll=30)
    jira.fail_all = (503, None)
    sync.tick()
    clock.now = sync.next_due("pay")
    sync.tick()
    jira.fail_all = None
    clock.now = sync.next_due("pay")
    # act
    sync.tick()
    # assert
    assert sync_state(app)["ok"] is True and sync.next_due("pay") == clock.now + 30


def test_sync__errors_emit_event_once_per_change(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, poll=30)
    sync.tick()
    jira.fail_all = (503, None)
    # act
    for _ in range(3):
        clock.now = sync.next_due("pay")
        sync.tick()
    # assert
    synced = events_of(app, "tracker_synced")
    assert [e["data"]["ok"] for e in synced] == [True, False]


def test_sync__secrets_never_reach_state_events_or_files(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    jira.fail_all = (400, {"errorMessages": ["bad token " + TRACKER_TOKEN]})
    # act
    sync.tick()
    clock.now += 30
    jira.fail_all = (401, {"errorMessages": [TRACKER_TOKEN]})
    sync.tick()
    # assert
    board = app.board_for("pay")
    everything = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                           for p in board.store.root.rglob("*") if p.is_file())
    everything += json.dumps(board.snapshot(), ensure_ascii=False)
    assert TRACKER_TOKEN not in everything and "tracker_sync" in everything


def test_sync__github_closed__marks_done(app, github, clock):
    # arrange
    sync = _github_project(app, github, clock, keys=("A",))
    sync.tick()
    github.issues[1]["state"] = "closed"
    clock.now += 120
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "done"


# --- синхронизация: исправления ревью ---

def test_sync__in_review_in_jira_maps_to_sent(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock, statuses={"In Review": "sent"})
    sync.tick()
    jira.statuses["PROJ-1"] = "In Review"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "sent"


def test_sync__forbidden_target_in_stored_config__is_skipped(app, jira, clock):
    # arrange: старая настройка, записанная в обход проверки
    sync = sync_project(app, jira, clock)
    board = app.board_for("pay")
    config = board.store.load_config()
    config.tracker["statuses"] = {"Open": None, "Done": "approved"}
    board.store.save_config(config)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "not_started"


def test_sync__missing_issue_in_jira__others_still_sync(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    del jira.statuses["PROJ-1"]
    jira.statuses["PROJ-2"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(app)
    assert task_status(app, "B") == "done" and state["ok"] is True and state["paused"] is False
    assert [e["key"] for e in state["errors"]] == ["A"] and "PROJ-1" in state["errors"][0]["error"]


@pytest.mark.parametrize("mode", ["header", "silent"])
def test_sync__jira_answers_as_anonymous__pauses_as_auth_error(app, jira, clock, mode):
    # arrange: просроченная cookie — Jira отвечает 200, но как анонимному пользователю
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.anonymous = mode
    clock.now += 30
    # act
    sync.tick()
    count = len(jira.requests)
    clock.now += 30
    sync.tick()
    # assert
    state = sync_state(app)
    assert state["ok"] is False and state["paused"] is True and "авторизац" in state["error"]
    assert jira.calls("GET", "/rest/api/2/myself") and len(jira.requests) == count
    assert task_status(app, "A") == "not_started"


def test_sync__all_issues_missing_but_user_known__not_paused(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.statuses.clear()
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(app)
    assert jira.calls("GET", "/rest/api/2/myself")
    assert state["ok"] is True and state["paused"] is False and sorted(e["key"] for e in state["errors"]) == ["A", "B"]


def test_sync__some_issues_found__does_not_ask_who_am_i(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    del jira.statuses["PROJ-1"]
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert not jira.calls("GET", "/rest/api/2/myself")


def test_check__jira_anonymous_header__is_auth_error(app, jira):
    # arrange
    with_jira(app, jira)
    jira.anonymous = "header"
    # act
    r = tracker.check_access(app.board_for("pay"), "user")
    # assert
    assert r["ok"] is False and r["kind"] == "auth"


def test_check_and_sync__plain_http_remote_tracker__warn(app, jira, clock, monkeypatch):
    # arrange: адрес поддельной Jira считается удалённым
    from agent_board import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "LOCAL_HOSTS", ())
    sync = sync_project(app, jira, clock)
    # act
    checked = tracker.check_access(app.board_for("pay"), "user")
    sync.tick()
    # assert
    assert checked["ok"] is True and "https" in checked["warning"]
    state = sync_state(app)
    assert state["ok"] is True and "https" in state["warning"]


def test_check_and_sync__localhost_tracker__no_warning(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    # act
    checked = tracker.check_access(app.board_for("pay"), "user")
    sync.tick()
    # assert
    assert checked == {"ok": True} and sync_state(app)["warning"] is None


def test_sync__jira_rejects_batch__retries_per_key(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    jira.reject_batches = True
    jira.strict_missing = True
    jira.statuses["PROJ-2"] = "Done"
    del jira.statuses["PROJ-1"]
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "B") == "done" and [e["key"] for e in sync_state(app)["errors"]] == ["A"]


def _github_project(app, gh, clock, keys=("A", "B", "C")):
    board = app.board_for("pay")
    board.update_config({"tracker": {"type": "github", "repo": "owner/name", "api_url": gh.url,
                                     "auth": {"type": "token_env", "env": "GITHUB_TOKEN"},
                                     "statuses": {"open": None, "closed": "done"}}})
    for key in keys:
        add_task(app, key)
    board.set_stage("tracker", "user")
    create_issues(app)
    board.set_stage("work", "user")
    sync = app.tracker_sync = TrackerSync(app, clock=clock)
    return sync


def test_sync__github_missing_gone_moved_issues__others_still_sync(app, github, clock):
    # arrange
    sync = _github_project(app, github, clock, keys=("A", "B", "C", "D"))
    sync.tick()
    del github.issues[1]
    github.gone.add(2)
    github.moved.add(3)
    github.issues[4]["state"] = "closed"
    clock.now += 120
    # act
    sync.tick()
    # assert
    state = sync_state(app)
    assert task_status(app, "D") == "done" and state["ok"] is True and state["paused"] is False
    assert sorted(e["key"] for e in state["errors"]) == ["A", "B", "C"]


def test_sync__github_default_interval_is_120_seconds(app, github, clock):
    # arrange
    sync = _github_project(app, github, clock, keys=("A",))
    # act
    sync.tick()
    # assert
    assert sync.next_due("pay") == 120


def test_sync__first_read_done__transitions(app, jira, clock):
    # arrange: задача уже закрыта в трекере к первому чтению
    sync = sync_project(app, jira, clock)
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "In Progress"
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "done" and task_status(app, "B") == "not_started"


def test_sync__changed_tracker_key__counts_as_first_read(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    board = app.board_for("pay")
    board.update_task("A", "system", {"tracker_key": "PROJ-9"})
    jira.statuses["PROJ-9"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(app, "A") == "done"


def test_sync__seen_drops_cancelled_tasks(app, jira, clock):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    board = app.board_for("pay")
    board.transition("B", Status.CANCELLED, "user")
    jira.statuses["PROJ-1"] = "In Progress"
    clock.now += 30
    # act
    sync.tick()
    # assert
    seen = board.store.load_meta()["tracker_seen"]
    assert set(seen) == {"A"} and seen["A"] == ["PROJ-1", "In Progress"]


def test_sync__task_moved_meanwhile__transition_is_skipped(app, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    board = app.board_for("pay")
    real = board.transition_if
    calls = []

    def racing(key, expected, to, actor):
        calls.append(expected)
        board.transition(key, Status.IN_PROGRESS, "user")
        return real(key, expected, to, actor)

    monkeypatch.setattr(board, "transition_if", racing)
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert calls == [Status.NOT_STARTED] and task_status(app, "A") == "in_progress"
    assert sync_state(app)["changed"] == []


def test_sync__stop_flag_between_requests__stops_promptly(app, github, clock):
    # arrange
    sync = _github_project(app, github, clock, keys=("A", "B", "C", "D"))
    before = len(github.requests)
    original = sync._stop.is_set
    sync._stop.set()
    # act
    sync.tick()
    # assert
    assert len(github.requests) == before and sync_state(app) is None
    assert original() is True


def test_sync__no_tracker__no_requests_and_no_state(app, clock):
    # arrange
    add_task(app, "A")
    app.board_for("pay").set_stage("work", "user")
    sync = app.tracker_sync = TrackerSync(app, clock=clock)
    # act
    sync.tick()
    # assert
    assert sync_state(app) is None
    with pytest.raises(ValueError):
        sync.sync_now(app.board_for("pay"), "user")


def test_sync__connection_refused__backs_off_and_does_not_pause(app, jira, clock):
    # arrange
    import socket as _socket

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    sync = sync_project(app, jira, clock, poll=30)
    board = app.board_for("pay")
    board.update_config({"tracker": dict(board.store.load_config().tracker, url="http://127.0.0.1:%d" % port)})
    delays = []
    # act
    for _ in range(3):
        sync.tick()
        delays.append(sync.next_due("pay") - clock.now)
        clock.now = sync.next_due("pay")
    # assert
    state = sync_state(app)
    assert delays == [30, 60, 120] and state["ok"] is False and state["paused"] is False


def test_sync__manual_sync_during_scheduled_tick__waits_and_applies_once(app, jira, clock):
    # arrange
    gate = threading.Event()

    class Gated(FakeJira):
        def route(self, method, path, body):
            if path.startswith("/rest/api/2/search") and getattr(self, "gated", False):
                gate.wait(5)
            return super().route(method, path, body)

    gated = Gated(authorization="Bearer " + TRACKER_TOKEN).start()
    try:
        gated.statuses = jira.statuses
        sync = sync_project(app, gated, clock)
        sync.tick()
        gated.statuses["PROJ-1"] = "Done"
        gated.gated = True
        clock.now += 30
        results = {}
        tick = threading.Thread(target=sync.tick)
        tick.start()
        while not any(r.path.startswith("/rest/api/2/search") for r in gated.requests[-1:]) and tick.is_alive():
            time.sleep(0.02)
        manual = threading.Thread(target=lambda: results.update(r=sync.sync_now(app.board_for("pay"), "user")))
        manual.start()
        time.sleep(0.2)
        waited = manual.is_alive()
        # act
        gate.set()
        tick.join(10)
        manual.join(10)
        # assert
        assert waited and results["r"]["ok"] is True
        changes = [e for e in events_of(app, "status_changed") if e["data"]["actor"] == "tracker"]
        assert len(changes) == 1 and task_status(app, "A") == "done"
    finally:
        gate.set()
        gated.stop()


# --- синхронизация: завершённые проекты и пул ---

class FakeWallClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def done_at(app):
    from datetime import datetime

    event = [e for e in events_of(app, "stage_changed") if e["data"]["to"] == "done"][-1]
    return datetime.fromisoformat(event["at"])


@pytest.mark.parametrize("hours, polled", [(23, True), (25, False)])
def test_sync__done_project__polled_for_24_hours_after_done(app, jira, clock, hours, polled):
    # arrange
    from datetime import timedelta

    sync = sync_project(app, jira, clock, stage="done")
    sync = app.tracker_sync = TrackerSync(app, clock=clock,
                                             wall_clock=FakeWallClock(done_at(app) + timedelta(hours=hours)))
    before = len(jira.requests)
    # act
    sync.tick()
    # assert
    assert (len(jira.requests) > before) is polled


def test_sync__done_time__remembered_in_meta(app, jira, clock):
    # arrange
    from datetime import timedelta

    sync_project(app, jira, clock, stage="done")
    sync = app.tracker_sync = TrackerSync(app, clock=clock,
                                             wall_clock=FakeWallClock(done_at(app) + timedelta(hours=1)))
    # act
    sync.tick()
    # assert
    meta = app.board_for("pay").store.load_meta()
    assert meta["stage_done_at"] == events_of(app, "stage_changed")[-1]["at"]


def _projects(*slugs):
    for slug in slugs:
        ProjectStore(slug).create(ProjectConfig(slug=slug, title=slug))


def test_sync__hung_project__does_not_delay_others(app, clock, monkeypatch):
    # arrange
    _projects("a", "b", "c")
    sync = app.tracker_sync = TrackerSync(app, clock=clock)
    sync.tick_wait = 0.3
    started, release = threading.Event(), threading.Event()
    ran = []

    def fake_tick_project(slug):
        if slug == "a":
            started.set()
            release.wait(10)
        ran.append(slug)

    monkeypatch.setattr(sync, "_tick_project", fake_tick_project)

    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    try:
        # act
        t0 = time.monotonic()
        sync.tick()
        first = time.monotonic() - t0
        sync.tick()
        # assert
        assert started.is_set() and first < 5
        assert sorted(ran) == ["b", "b", "c", "c", "pay", "pay"]
    finally:
        release.set()
        sync.stop()
    assert ran.count("a") == 1


def test_sync__projects_run_in_pool_of_at_most_4(app, clock, monkeypatch):
    # arrange
    _projects("a", "b", "c", "d", "e")
    sync = app.tracker_sync = TrackerSync(app, clock=clock)
    sync.tick_wait = 0.3
    guard, release = threading.Lock(), threading.Event()
    state = {"active": 0, "peak": 0}
    ran = []

    def fake_tick_project(slug):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        release.wait(10)
        with guard:
            state["active"] -= 1
            ran.append(slug)

    monkeypatch.setattr(sync, "_tick_project", fake_tick_project)

    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    try:
        # act
        sync.tick()
        peak_while_hung = state["peak"]
        release.set()
        deadline = time.monotonic() + 10
        while len(ran) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        sync.tick()
    finally:
        release.set()
        sync.stop()
    # assert: проекты, которым не хватило места, проходят следующим тиком
    assert peak_while_hung == 4 and state["peak"] == 4
    assert sorted(ran[:4]) == ["a", "b", "c", "d"] and set(ran) == {"a", "b", "c", "d", "e", "pay"}


def test_sync__stop__returns_promptly_with_hung_project(app, clock, monkeypatch):
    # arrange
    sync = app.tracker_sync = TrackerSync(app, clock=clock, sleep=lambda s: time.sleep(0.01))
    sync.tick_wait = 0.2
    release = threading.Event()
    monkeypatch.setattr(sync, "_tick_project", lambda slug: release.wait(30))
    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    sync.start()
    time.sleep(0.1)
    # act
    t0 = time.monotonic()
    sync.stop()
    elapsed = time.monotonic() - t0
    release.set()
    # assert
    assert elapsed < 5


def test_sync_tick__no_project_is_due__starts_no_worker_threads(app, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    started = []
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: started.append(slug))
    # act
    sync.tick()
    # assert
    assert started == []


def test_sync_tick__project_is_due__starts_worker(app, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(app, jira, clock)
    sync.tick()
    clock.now += 31
    started = []
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: (started.append(slug), lock.release(), sync._slots.release()))
    # act
    sync.tick()
    # assert
    assert started == ["pay"]


