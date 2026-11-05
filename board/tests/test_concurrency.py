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
                board.update_task(key, "agent", summary="%s-%d" % (key, n))
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
