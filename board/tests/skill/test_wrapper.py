from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "skills" / "agent-board" / "scripts" / "agent-board"


def pyproject_version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("version не найдена")


@pytest.fixture
def home(tmp_home):
    yield tmp_home
    # каждый запущенный сервер останавливается: штатно, затем принудительно
    run(["stop"], tmp_home)
    try:
        pid = json.loads((tmp_home / "server.json").read_text(encoding="utf-8"))["pid"]
        os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError, KeyError):
        pass


def run(args, home, wrapper=WRAPPER, cwd=None, env=None, timeout=60):
    full_env = dict(os.environ, AGENT_BOARD_HOME=str(home), AGENT_BOARD_PYTHON=sys.executable)
    full_env.update(env or {})
    return subprocess.run([str(wrapper)] + list(args), capture_output=True, text=True, timeout=timeout,
                          cwd=str(cwd) if cwd else None, env=full_env)


def discovery(home) -> dict:
    return json.loads((home / "server.json").read_text(encoding="utf-8"))


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --- обёртка ---

def test_wrapper__other_cwd__runs_version(tmp_path, home):
    # act
    r = run(["version"], home, cwd=tmp_path)
    # assert
    assert r.returncode == 0 and r.stdout.strip() == pyproject_version()


def test_wrapper__through_symlink__runs_version(tmp_path, home):
    # arrange
    link = tmp_path / "bin" / "ab"
    link.parent.mkdir()
    link.symlink_to(WRAPPER)
    outer = tmp_path / "outer"
    outer.symlink_to(link)
    # act
    r = run(["version"], home, wrapper=outer, cwd=tmp_path)
    # assert
    assert r.returncode == 0 and r.stdout.strip() == pyproject_version()


def test_wrapper__relative_symlink__runs_version(tmp_path, home):
    # arrange
    link = tmp_path / "ab"
    link.symlink_to(os.path.relpath(str(WRAPPER), str(tmp_path)))
    # act
    r = run(["version"], home, wrapper=link, cwd=tmp_path)
    # assert
    assert r.returncode == 0 and r.stdout.strip() == pyproject_version()


def test_wrapper__copied_outside_repo__explains_how_to_install(tmp_path, home):
    # arrange: каталог скилла скопирован без репозитория
    copy = tmp_path / "skills" / "agent-board" / "scripts" / "agent-board"
    copy.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, copy)
    # act
    r = run(["version"], home, wrapper=copy, cwd=tmp_path)
    # assert
    assert r.returncode != 0 and "install-codex.sh" in r.stderr and "No module" not in r.stderr


def test_wrapper__python_missing__clear_error(home):
    # act
    r = run(["version"], home, env={"AGENT_BOARD_PYTHON": "no-such-python-binary"})
    # assert
    assert r.returncode == 127 and "not found" in r.stderr and "AGENT_BOARD_PYTHON" in r.stderr


def test_wrapper__python_too_old__clear_error(tmp_path, home):
    # arrange
    old = tmp_path / "python-old"
    old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    old.chmod(0o755)
    # act
    r = run(["version"], home, env={"AGENT_BOARD_PYTHON": str(old)})
    # assert
    assert r.returncode == 127 and "3.9" in r.stderr


def test_wrapper__pythonpath_set__is_preserved(tmp_path, home):
    # act
    r = run(["version"], home, env={"PYTHONPATH": str(tmp_path)})
    # assert
    assert r.returncode == 0 and r.stdout.strip() == pyproject_version()


def test_version__package_matches_pyproject():
    # arrange
    from agent_board import __version__

    # assert
    assert __version__ == pyproject_version()


# --- status ---

def test_status__no_server__reports_not_running_and_projects(home):
    # arrange
    assert run(["init", "pay", "--title", "Платежи"], home).returncode == 0
    # act
    human = run(["status"], home)
    as_json = run(["status", "--json"], home)
    # assert
    assert human.returncode == 0 and "не запущена" in human.stdout and "pay" in human.stdout
    assert json.loads(as_json.stdout) == {"server": None, "projects": [
        {"slug": "pay", "title": "Платежи", "stage": "decomposition", "tasks": 0}]}


def test_status__stale_discovery__reports_not_running(home):
    # arrange
    (home / "server.json").write_text(json.dumps({"port": 1, "token": "t", "pid": 2 ** 22 + 12345}),
                                      encoding="utf-8")
    # act
    r = run(["status", "--json"], home)
    # assert
    assert json.loads(r.stdout)["server"] is None


def test_status__task_transition_form__still_requires_server(home):
    # act
    r = run(["--project", "pay", "status", "T-1", "in_progress"], home)
    # assert
    assert r.returncode == 2


def test_status__single_argument__usage_error(home):
    # act
    r = run(["status", "T-1"], home)
    # assert
    assert r.returncode == 1 and "status" in r.stderr


# --- serve --detach / stop ---

def test_detach__not_running__starts_server_and_prints_url(home):
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    info = discovery(home)
    assert r.returncode == 0 and r.stdout.strip() == "http://127.0.0.1:%d/?token=%s" % (info["port"], info["token"])
    assert info["version"] == pyproject_version() and alive(info["pid"])
    assert (home / "server.log").exists()


def test_detach__server_in_new_session__survives_parent(home):
    # act
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    pid = discovery(home)["pid"]
    # assert
    assert os.getsid(pid) == pid != os.getsid(0)


def test_detach__passes_flags_through(home):
    # act
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(discovery(home)["pid"])], capture_output=True,
                         text=True).stdout
    # assert
    assert "--no-runner" in cmd and "--no-tracker-sync" in cmd and "agent_board serve" in cmd


def test_detach__explicit_port__uses_it(home):
    # arrange
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync", "--port", str(port)], home)
    # assert
    assert r.returncode == 0 and discovery(home)["port"] == port


def test_detach__same_version_running__prints_same_url_exit_0(home):
    # arrange
    first = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    pid = discovery(home)["pid"]
    # act
    second = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert second.returncode == 0 and second.stdout == first.stdout and discovery(home)["pid"] == pid


def test_detach__other_version_running__exit_3_with_restart_hint(home):
    # arrange
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    info = discovery(home)
    (home / "server.json").write_text(json.dumps(dict(info, version="0.0.1")), encoding="utf-8")
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert r.returncode == 3 and "agent-board stop && agent-board serve --detach" in r.stderr
    assert alive(info["pid"])


def test_detach__server_without_version_in_discovery__treated_as_other_version(home):
    # arrange
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    info = discovery(home)
    info.pop("version")
    (home / "server.json").write_text(json.dumps(info), encoding="utf-8")
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert r.returncode == 3


def test_detach__port_taken__fails_with_log_tail_exit_1(home):
    # arrange
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        # act
        r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync", "--port", str(port)], home)
    # assert
    assert r.returncode == 1 and "server.log" in r.stderr and "Address already in use" in r.stderr
    assert not (home / "server.json").exists()


def test_detach__big_log__trimmed_to_limit_before_start(home):
    # arrange
    log = home / "server.log"
    log.write_bytes((b"x" * 99 + b"\n") * (6 * 1024 * 1024 // 100))
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert r.returncode == 0 and log.stat().st_size <= 5 * 1024 * 1024 + 1024


def test_detach__stale_discovery_after_crash__starts_new_server(home):
    # arrange
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    old = discovery(home)["pid"]
    os.kill(old, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while alive(old) and time.monotonic() < deadline:
        time.sleep(0.05)
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert r.returncode == 0 and discovery(home)["pid"] != old and alive(discovery(home)["pid"])


def test_status__running_server__reports_url_version_pid(home):
    # arrange
    started = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    info = discovery(home)
    # act
    r = run(["status", "--json"], home)
    human = run(["status"], home)
    # assert
    assert json.loads(r.stdout)["server"] == {"running": True, "url": started.stdout.strip(),
                                              "version": pyproject_version(), "pid": info["pid"]}
    assert "запущена" in human.stdout and str(info["pid"]) in human.stdout


def test_stop__running_server__stops_and_removes_discovery(home):
    # arrange
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    pid = discovery(home)["pid"]
    # act
    r = run(["stop"], home)
    # assert
    assert r.returncode == 0 and not (home / "server.json").exists() and not alive(pid)


def test_stop__no_server__exit_0_with_message(home):
    # act
    r = run(["stop"], home)
    # assert
    assert r.returncode == 0 and "не запущена" in r.stdout


def test_stop__pid_is_foreign_process__refuses_and_keeps_process(home):
    # arrange
    foreign = subprocess.Popen(["sleep", "30"])
    try:
        (home / "server.json").write_text(json.dumps({"port": 1, "token": "t", "pid": foreign.pid}),
                                          encoding="utf-8")
        # act
        r = run(["stop"], home)
        # assert
        assert r.returncode == 1 and "не похож" in r.stderr and foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()
        (home / "server.json").unlink()


def test_stop__then_detach__starts_again(home):
    # arrange
    run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    run(["stop"], home)
    # act
    r = run(["serve", "--detach", "--no-runner", "--no-tracker-sync"], home)
    # assert
    assert r.returncode == 0 and alive(discovery(home)["pid"])


# --- fix round 1 ---

class _FakeChild:
    pid = 2 ** 22 + 777

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def _fake_popen(monkeypatch, lifecycle, popen):
    import types

    # подменяется только модуль в lifecycle: глобальный subprocess нужен фикстурам
    monkeypatch.setattr(lifecycle, "subprocess", types.SimpleNamespace(
        Popen=popen, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired))


def test_start_detached__child_lost_lock_race__waits_for_winner_discovery(home, monkeypatch):
    # arrange
    from agent_board import __version__, lifecycle

    started = time.monotonic()
    info = {"port": 1, "token": "t", "pid": 1, "version": __version__}
    _fake_popen(monkeypatch, lifecycle, lambda *a, **k: _FakeChild(3))
    monkeypatch.setattr(lifecycle, "live_server", lambda: info if time.monotonic() - started > 0.3 else None)
    # act
    result = lifecycle.start_detached(None, True, True)
    # assert
    assert result == {"url": "http://127.0.0.1:1/?token=t"}


def test_start_detached__child_lost_race_winner_never_appears__fails(home, monkeypatch):
    # arrange
    from agent_board import lifecycle

    _fake_popen(monkeypatch, lifecycle, lambda *a, **k: _FakeChild(3))
    monkeypatch.setattr(lifecycle, "live_server", lambda: None)
    monkeypatch.setattr(lifecycle, "START_WAIT_SECONDS", 0.3)
    # act
    result = lifecycle.start_detached(None, True, True)
    # assert
    assert result["code"] == 1


def test_start_detached__agent_env__not_passed_to_server(home, monkeypatch):
    # arrange
    from agent_board import lifecycle

    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs["env"])
        return _FakeChild(1)

    for name in lifecycle.AGENT_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("KEEP_ME", "1")
    _fake_popen(monkeypatch, lifecycle, fake_popen)
    monkeypatch.setattr(lifecycle, "START_WAIT_SECONDS", 0.3)
    # act
    lifecycle.start_detached(None, True, True)
    # assert
    assert seen["KEEP_ME"] == "1" and not set(lifecycle.AGENT_ENV) & set(seen)


@pytest.mark.parametrize("command, expected", [
    ("/usr/bin/python3 -m agent_board serve --port 1", True),
    ("python3 -m agent_board --project x serve", False),
    ("/usr/bin/python3 /opt/bin/agent-board serve", True),
    ("/x/agent-board/node_modules/.bin/node /x/agent-board/node_modules/.bin/vite serve", False),
    ("node /home/u/agent-board/ui-tests/node_modules/vite/bin/vite.js serve", False),
    ("python3 -m agent_board status", False),
])
def test_is_board_server__command_line__strict_match(monkeypatch, command, expected):
    # arrange
    from agent_board import lifecycle

    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: {"started": "x", "command": command})
    # act / assert
    assert lifecycle._is_board_server(1) is expected


def test_trim_log__start_lock_held__skipped(home):
    # arrange
    import fcntl

    from agent_board import lifecycle

    log = home / "server.log"
    log.write_bytes((b"x" * 99 + b"\n") * (6 * 1024 * 1024 // 100))
    size = log.stat().st_size
    fd = os.open(str(home / "server.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        # act
        lifecycle._trim_log_unlocked(log, home)
        held = log.stat().st_size
    finally:
        os.close(fd)
    lifecycle._trim_log_unlocked(log, home)
    # assert
    assert held == size and log.stat().st_size < size
