from __future__ import annotations

import pytest

from agent_board.model import Task


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BOARD_HOME", str(tmp_path))
    return tmp_path


def make_task(key, **kw) -> Task:
    kw.setdefault("title", key)
    return Task(key=key, **kw)
