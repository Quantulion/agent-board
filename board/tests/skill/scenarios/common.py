"""Общее для make_env.py и check_state.py: запуск обёртки скилла в изолированном окружении."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WRAPPER = ROOT / "skills" / "agent-board" / "scripts" / "agent-board"
DEMO_SLUG = "demo"


def board_env(env_dir: Path) -> dict:
    env = dict(os.environ, AGENT_BOARD_HOME=str(env_dir / "home"), AGENT_BOARD_PYTHON=sys.executable)
    for name in ("AGENT_BOARD_PROJECT", "AGENT_BOARD_TASK", "AGENT_BOARD_ACTOR"):
        env.pop(name, None)
    return env


def cli(env_dir: Path, args, check=True, timeout=60):
    res = subprocess.run([str(WRAPPER)] + [str(a) for a in args], capture_output=True, text=True,
                         timeout=timeout, env=board_env(env_dir), cwd=str(env_dir))
    if check and res.returncode != 0:
        raise RuntimeError("agent-board %s: код %s: %s" % (" ".join(map(str, args)), res.returncode, res.stderr.strip()))
    return res


def cli_json(env_dir: Path, args):
    return json.loads(cli(env_dir, args).stdout)


def server_running(env_dir: Path) -> bool:
    return bool((cli_json(env_dir, ["status", "--json"]).get("server") or {}).get("running"))
