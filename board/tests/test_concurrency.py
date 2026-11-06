from __future__ import annotations

import json
import threading

from conftest import make_task as mk

THREADS = 20
ITERATIONS = 25


def test_parallel_post_and_update_keep_log_and_files_consistent(board):
    # arrange
    keys = ["T%d" % i for i in range(THREADS)]
    for key in keys:
        board.add_task(mk(key), "user")
    barrier = threading.Barrier(THREADS)
    errors = []

    def work(key):
        try:
            barrier.wait()
            for n in range(ITERATIONS):
                board.post_message(key, "agent", "%s-%d" % (key, n))
                board.update_task(key, "agent", {"summary": "%s-%d" % (key, n)})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    workers = [threading.Thread(target=work, args=(k,)) for k in keys]

    # act
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    # assert
    assert errors == []
    store = board.store
    assert sum(len(store.read_thread(k)) for k in keys) == THREADS * ITERATIONS
    seqs = [e.seq for e in store.read_events()]
    assert seqs == list(range(1, len(seqs) + 1))
    for key in keys:
        with open(store.root / "tasks" / (key + ".json"), encoding="utf-8") as f:
            assert json.load(f)["summary"] == "%s-%d" % (key, ITERATIONS - 1)


def test_config_set_and_runner_session_write_concurrently__both_persist(tmp_home, monkeypatch):
    # arrange: чтение настроек медленное, чтобы записи пересеклись без блокировки доски
    import threading
    import time

    from agent_board.board import Board
    from agent_board.config import ProjectConfig
    from agent_board.store import ProjectStore

    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи"))
    board = Board(store)
    real_load = store.load_config

    def slow_load():
        config = real_load()
        time.sleep(0.05)
        return config

    monkeypatch.setattr(store, "load_config", slow_load)
    errors = []

    def runner_write():
        try:
            for i in range(5):
                board.update_config({"project_session_id": "session-%d" % i})
        except Exception as e:  # pragma: no cover - диагностика
            errors.append(e)

    def user_write():
        try:
            for i in range(5):
                board.set_config("user", {"max_agents": i + 1, "agent_rules": "правило %d" % i})
        except Exception as e:  # pragma: no cover - диагностика
            errors.append(e)

    threads = [threading.Thread(target=runner_write), threading.Thread(target=user_write)]
    # act
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    # assert
    final = real_load()
    assert errors == []
    assert final.project_session_id == "session-4" and final.max_agents == 5 and final.agent_rules == "правило 4"
