from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .harness import process_identity
from .review import trim_log
from .server import _port_open, discovery_path, read_discovery
from .store import ProjectStore, board_home, list_projects

LOG_LIMIT_BYTES = 5 * 1024 * 1024
START_WAIT_SECONDS = 10.0
STOP_WAIT_SECONDS = 15.0
LOG_TAIL_LINES = 20
RESTART_HINT = "перезапустите: agent-board stop && agent-board serve --detach"


def server_log_path() -> Path:
    return board_home() / "server.log"


def live_server() -> Optional[dict]:
    """Параметры работающего сервера (процесс жив и порт отвечает) или None."""
    info = read_discovery()
    if info is None or not _port_open(info.get("port")):
        return None
    return info


def server_url(info: dict) -> str:
    return "http://127.0.0.1:%d/?token=%s" % (info["port"], info["token"])


def status_report() -> dict:
    """Состояние сервера и проектов; читает файлы, сервер для этого не нужен."""
    info = live_server()
    server = None
    if info is not None:
        server = {"running": True, "url": server_url(info), "version": info.get("version"), "pid": info.get("pid")}
    projects = []
    for slug in list_projects():
        try:
            store = ProjectStore(slug)
            config = store.load_config()
            projects.append({"slug": slug, "title": config.title, "stage": config.stage,
                             "tasks": len(store.load_tasks())})
        except (OSError, ValueError, KeyError):
            projects.append({"slug": slug, "title": None, "stage": None, "tasks": None})
    return {"server": server, "projects": projects}


def format_status(report: dict) -> str:
    server = report["server"]
    lines = []
    if server is None:
        lines.append("доска не запущена (agent-board serve --detach)")
    else:
        lines.append("доска запущена: %s (версия %s, pid %s)" % (server["url"], server["version"] or "?",
                                                                server["pid"]))
    for p in report["projects"]:
        if p["stage"] is None:
            lines.append("  %s: не удалось прочитать проект" % p["slug"])
        else:
            lines.append("  %s «%s»: стадия %s, задач %d" % (p["slug"], p["title"], p["stage"], p["tasks"]))
    if not report["projects"]:
        lines.append("  проектов нет")
    return "\n".join(lines)


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL_LINES:])


def start_detached(port: Optional[int], no_runner: bool, no_tracker_sync: bool) -> dict:
    """Запускает сервер в фоне и ждёт его готовности. Возвращает {"url": ...} или {"error": ..., "code": N}."""
    running = live_server()
    if running is not None:
        if running.get("version") == __version__:
            return {"url": server_url(running)}
        return {"error": "доска уже запущена другой версии (%s, нужна %s): %s"
                         % (running.get("version") or "неизвестна", __version__, RESTART_HINT), "code": 3}
    home = board_home()
    home.mkdir(parents=True, exist_ok=True)
    log = server_log_path()
    trim_log(log, LOG_LIMIT_BYTES)
    cmd: List[str] = [sys.executable, "-m", "agent_board", "serve"]
    if port is not None:
        cmd += ["--port", str(port)]
    if no_runner:
        cmd.append("--no-runner")
    if no_tracker_sync:
        cmd.append("--no-tracker-sync")
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = package_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(str(log), "ab") as out:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                                 start_new_session=True)
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        info = read_discovery()
        if info is not None and info.get("pid") == child.pid and _port_open(info.get("port")):
            return {"url": server_url(info)}
        code = child.poll()
        if code is not None:
            # гонка с другим запуском: сервер уже поднят
            info = live_server()
            if code == 3 and info is not None:
                return {"url": server_url(info)}
            return {"error": "сервер не запустился (код %s), %s:\n%s" % (code, log, _log_tail(log)), "code": 1}
        time.sleep(0.05)
    _terminate(child)
    return {"error": "сервер не поднялся за %g с, %s:\n%s" % (START_WAIT_SECONDS, log, _log_tail(log)), "code": 1}


def _terminate(child) -> None:
    try:
        child.terminate()
        child.wait(5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    except OSError:
        pass


def _is_board_server(pid: int) -> bool:
    identity = process_identity(pid)
    if identity is None:
        return False
    words = identity["command"].replace("-", "_").split()
    return "agent_board" in identity["command"].replace("-", "_") and "serve" in words


def stop_server() -> dict:
    """Останавливает сервер по pid из discovery после проверки, что это процесс доски. {"message", "code"}."""
    info = read_discovery()
    if info is None:
        return {"message": "доска не запущена", "code": 0}
    pid = int(info["pid"])
    if not _is_board_server(pid):
        return {"message": "pid %d из %s не похож на сервер доски, процесс не тронут" % (pid, discovery_path()),
                "code": 1}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"message": "доска остановлена", "code": 0}
    except OSError as e:
        return {"message": "не удалось остановить pid %d: %s" % (pid, e), "code": 1}
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            # сервер — наш дочерний процесс (запуск и остановка из одного процесса): не оставляем зомби
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        if not discovery_path().exists() or read_discovery() is None:
            return {"message": "доска остановлена", "code": 0}
        time.sleep(0.1)
    return {"message": "доска (pid %d) не остановилась за %g с" % (pid, STOP_WAIT_SECONDS), "code": 1}
