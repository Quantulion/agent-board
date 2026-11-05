from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board.model import Status, Task
from agent_board.resources import ResourceQueue
from agent_board.server import BoardServer, read_discovery
from agent_board.store import ProjectStore

PID = os.getpid()


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def queue(tmp_home):
    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи", resources=[{"name": "tests", "description": "функциональные тесты"}]))
    board = Board(store)
    for key in ("T1", "T2", "T3"):
        board.add_task(Task(key=key, title=key), "user")
    return ResourceQueue(board)


def events(queue, type):
    return [e.data for e in queue._board.store.read_events() if e.type == type]


def status(queue, key):
    return queue._board.store.load_tasks()[key].status


def test_acquire_second_holder_waits_until_release(queue):
    # arrange
    assert queue.acquire("tests", "T1", PID) == 0
    # act
    second = queue.acquire("tests", "T2", PID)
    queue.release("tests", "T1", PID)
    # assert
    assert second == 1
    assert queue.poll("tests", "T2", PID) == 0
    assert events(queue, "lock_waiting") == [{"key": "T2", "resource": "tests", "position": 1}]
    assert [e["key"] for e in events(queue, "lock_acquired")] == ["T1", "T2"]
    assert events(queue, "lock_released") == [{"key": "T1", "resource": "tests"}]


def test_acquire_same_task_and_pid_is_idempotent(queue):
    # arrange
    queue.acquire("tests", "T1", PID)
    queue.acquire("tests", "T2", PID)
    # act
    again = queue.acquire("tests", "T2", PID)
    # assert
    assert again == 1
    assert len(events(queue, "lock_waiting")) == 1


def test_reap_frees_resource_of_dead_pid_for_next(queue):
    # arrange
    assert queue.acquire("tests", "T1", dead_pid()) == 0
    # act
    position = queue.acquire("tests", "T2", PID)
    # assert
    assert position == 0
    assert events(queue, "lock_released") == [{"key": "T1", "resource": "tests"}]


def test_fifo_order_between_waiters(queue):
    # arrange
    queue.acquire("tests", "T1", PID)
    queue.acquire("tests", "T2", PID)
    queue.acquire("tests", "T3", PID)
    # act
    queue.release("tests", "T1", PID)
    # assert
    assert queue.poll("tests", "T2", PID) == 0
    assert queue.poll("tests", "T3", PID) == 1


def test_waiting_task_is_queued_then_restored(queue):
    # arrange
    queue._board.transition("T2", Status.IN_PROGRESS, "user")
    queue.acquire("tests", "T1", PID)
    # act
    queue.acquire("tests", "T2", PID)
    during = status(queue, "T2")
    queue.release("tests", "T1", PID)
    queue.poll("tests", "T2", PID)
    # assert
    assert during == Status.QUEUED
    assert status(queue, "T2") == Status.IN_PROGRESS
    changes = [e for e in events(queue, "status_changed") if e["key"] == "T2" and e["actor"] == "system"]
    assert [(e["from"], e["to"]) for e in changes] == [("in_progress", "queued"), ("queued", "in_progress")]


def test_status_moved_by_user_while_waiting_is_left_alone(queue):
    # arrange
    queue._board.transition("T2", Status.IN_PROGRESS, "user")
    queue.acquire("tests", "T1", PID)
    queue.acquire("tests", "T2", PID)
    queue._board.transition("T2", Status.NOT_STARTED, "user")
    moved = status(queue, "T2")
    # act
    queue.release("tests", "T1", PID)
    queue.poll("tests", "T2", PID)
    # assert
    assert status(queue, "T2") == moved


def test_waiter_leaving_queue_restores_status(queue):
    # arrange
    queue.acquire("tests", "T1", PID)
    queue.acquire("tests", "T2", PID)
    # act
    queue.release("tests", "T2", PID)
    # assert
    assert status(queue, "T2") == Status.NOT_STARTED
    assert events(queue, "lock_released") == []


def test_queue_survives_restart_and_restores_status(queue):
    # arrange
    queue._board.transition("T2", Status.IN_PROGRESS, "user")
    queue.acquire("tests", "T1", PID)
    queue.acquire("tests", "T2", PID)
    # act
    reborn = ResourceQueue(queue._board)
    position = reborn.acquire("tests", "T3", PID)
    reborn.release("tests", "T1", PID)
    # assert
    assert position == 2
    assert reborn.poll("tests", "T2", PID) == 0
    assert status(queue, "T2") == Status.IN_PROGRESS
    assert [e for e in events(queue, "lock_waiting") if e["key"] == "T2"] == [{"key": "T2", "resource": "tests", "position": 1}]


def test_task_waiting_on_two_resources_stays_queued_until_both_free(queue):
    # arrange
    config = queue._board.store.load_config()
    config.resources.append({"name": "stand", "description": ""})
    queue._board.store.save_config(config)
    queue.acquire("tests", "T1", PID)
    queue.acquire("stand", "T3", PID)
    queue.acquire("tests", "T2", PID)
    queue.acquire("stand", "T2", PID)
    # act
    queue.release("tests", "T1", PID)
    queue.poll("tests", "T2", PID)
    after_first = status(queue, "T2")
    queue.release("stand", "T3", PID)
    queue.poll("stand", "T2", PID)
    # assert
    assert after_first == Status.QUEUED
    assert status(queue, "T2") == Status.NOT_STARTED


def test_undeclared_resource_raises_value_error_listing_declared(queue):
    # act / assert
    with pytest.raises(ValueError, match="tests"):
        queue.acquire("stand", "T1", PID)


def test_unknown_task_raises_key_error(queue):
    with pytest.raises(KeyError):
        queue.acquire("tests", "NOPE", PID)


# --- API ---

@pytest.fixture
def srv(tmp_home):
    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи", resources=[{"name": "tests", "description": ""}]))
    Board(store).add_task(Task(key="T1", title="x"), "user")
    server = BoardServer(port=0)
    server.start()
    yield server
    server.stop()


def post(action, resource, body):
    info = read_discovery()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/p/pay/locks/%s/%s" % (info["port"], resource, action),
        data=json.dumps(body).encode(), method="POST",
        headers={"X-Board-Token": info["token"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_acquire_poll_release_roundtrip(srv):
    # act
    acquired = post("acquire", "tests", {"task": "T1", "pid": PID})
    polled = post("poll", "tests", {"task": "T1", "pid": PID})
    released = post("release", "tests", {"task": "T1", "pid": PID})
    # assert
    assert acquired == (200, {"position": 0})
    assert polled == (200, {"position": 0})
    assert released[0] == 200


def test_api_undeclared_resource_is_400_with_declared_list(srv):
    # act
    code, body = post("acquire", "stand", {"task": "T1", "pid": PID})
    # assert
    assert code == 400 and "tests" in body["error"]


def test_api_unknown_task_is_404(srv):
    assert post("acquire", "tests", {"task": "NOPE", "pid": PID})[0] == 404
