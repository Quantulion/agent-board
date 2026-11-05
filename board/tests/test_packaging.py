from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_module_entry_point_runs_from_repo_root():
    # arrange
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH="board")

    # act
    r = subprocess.run([sys.executable, "-m", "agent_board", "--help"], cwd=str(root), env=env,
                       capture_output=True, text=True, timeout=30)

    # assert
    assert r.returncode == 0 and "serve" in r.stdout


def test_pyproject_declares_console_script():
    # arrange
    root = Path(__file__).resolve().parents[2]

    # act
    text = (root / "pyproject.toml").read_text(encoding="utf-8")

    # assert
    assert '[project]' in text and 'name = "agent-board"' in text
    assert 'agent-board = "agent_board.cli:main"' in text
