from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

DEFAULT_SYSTEM_PROMPT = (
    "Ты работаешь над задачей доски agent-board. Не делай git commit и git push. "
    "Статусы, вопросы и отчёт передавай командой agent-board."
)
STDERR_TAIL = 2000


@dataclass
class TurnResult:
    session_id: Optional[str]
    ok: bool
    error: str = ""
    exit_code: int = 0
    note: str = ""


class FailedProc:
    """Заглушка вместо процесса, который не удалось запустить."""

    stdout = None
    returncode = 127
    pid = None

    def __init__(self, error: str, session_hint: Optional[str] = None):
        self.error = error
        self.session_hint = session_hint

    def wait(self, timeout=None):
        return self.returncode


class Harness:
    binary = ""

    def new_session_id(self) -> Optional[str]:
        return None

    def start_turn(self, cwd: Path, prompt: str, session_id: Optional[str], log: Path, env: dict):
        raise NotImplementedError

    def resume_turn(self, cwd: Path, session_id: str, message: str, log: Path, env: dict):
        raise NotImplementedError

    def terminal_command(self, cwd: Path, session_id: str) -> str:
        raise NotImplementedError

    # --- общая механика ---

    def _spawn(self, args: List[str], cwd: Path, env: dict, session_hint: Optional[str], stdin_text: str = ""):
        full_env = dict(os.environ)
        full_env.update(env)
        if shutil.which(args[0], path=full_env.get("PATH")) is None:
            return FailedProc("%s не найден в PATH" % args[0], session_hint)
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd), env=full_env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                start_new_session=True,
            )
        except OSError as e:
            return FailedProc("не удалось запустить %s: %s" % (args[0], e), session_hint)
        proc.session_hint = session_hint
        # Промпт идёт через stdin отдельным потоком: большой ввод не должен блокировать чтение вывода.
        threading.Thread(target=_feed_stdin, args=(proc, stdin_text), daemon=True).start()
        return proc

    def finish(self, proc, log: Path) -> TurnResult:
        hint = getattr(proc, "session_hint", None)
        if isinstance(proc, FailedProc):
            return TurnResult(session_id=hint, ok=False, error=proc.error, exit_code=proc.returncode)
        state = {"session_id": hint, "error": "", "note": ""}
        tail = ""
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(str(log), "a", encoding="utf-8") as out:
                for line in proc.stdout:
                    out.write(line if line.endswith("\n") else line + "\n")
                    out.flush()
                    event = _parse(line)
                    if event is None:
                        tail = (tail + line)[-STDERR_TAIL:]
                    else:
                        self._on_event(event, state)
            code = proc.wait()
        except BaseException:
            _kill_group(proc)
            raise
        error = state["error"]
        if not error and code != 0:
            error = tail.strip() or "процесс завершился с кодом %d" % code
        return TurnResult(
            session_id=state["session_id"], ok=not error and code == 0, error=error, exit_code=code, note=state["note"]
        )

    def _on_event(self, event: dict, state: dict) -> None:
        raise NotImplementedError


def _feed_stdin(proc, text: str) -> None:
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):
        pass


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait()
    except Exception:
        pass


def terminate(proc, grace: float) -> None:
    """Останавливает группу процессов хода: SIGTERM, через grace секунд SIGKILL."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity(pid: int) -> Optional[dict]:
    """Время запуска и командная строка процесса или None, если процесса нет."""
    try:
        res = subprocess.run(["ps", "-o", "lstart=", "-o", "command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    line = res.stdout.strip()
    if res.returncode != 0 or not line:
        return None
    # lstart — пять полей: «Thu Nov  5 19:09:21 2026»
    parts = line.split(None, 5)
    if len(parts) < 6:
        return None
    return {"started": " ".join(parts[:5]), "command": parts[5]}


def turn_process_record(proc, session_id: Optional[str]) -> Optional[dict]:
    """Опознавательные данные процесса хода, чтобы после перезапуска не задеть чужой процесс с тем же pid."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return None
    args = [str(a) for a in (getattr(proc, "args", None) or [])]
    joined = " ".join(args)
    mark = session_id if session_id and session_id in joined else (Path(args[0]).name if args else "")
    identity = process_identity(pid)
    return {"pgid": pid, "started": identity["started"] if identity else None, "mark": mark}


def is_same_process(record: dict) -> bool:
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or pgid <= 0 or not record.get("started") or not record.get("mark"):
        return False
    identity = process_identity(pgid)
    return (identity is not None and identity["started"] == record["started"]
            and record["mark"] in identity["command"])


def terminate_group(pgid: int, grace: float, poll: float = 0.05) -> bool:
    """Останавливает чужую (не нашу дочернюю) группу процессов; True, если группы больше нет."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return True
            time.sleep(poll)
    return not group_alive(pgid)


def _parse(line: str) -> Optional[dict]:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


# Подкоманды доски, разрешённые агенту в любом режиме; lock-run запускает произвольную команду
# и потому подчиняется permission_mode, как любая другая.
BOARD_CLI_SUBCOMMANDS = ("status", "update", "ask", "say", "show", "thread", "state", "events", "propose")
BOARD_CLI_PERMISSIONS = ["Bash(agent-board %s:*)" % name for name in BOARD_CLI_SUBCOMMANDS]


class ClaudeHarness(Harness):
    binary = "claude"

    def __init__(self, permission_mode: str, model: Optional[str] = None, system_prompt: str = DEFAULT_SYSTEM_PROMPT):
        self.permission_mode = permission_mode
        self.model = model
        self.system_prompt = system_prompt

    def new_session_id(self) -> Optional[str]:
        return str(uuid.uuid4())

    def _common(self) -> List[str]:
        return ["--output-format", "stream-json", "--verbose", "--permission-mode", self.permission_mode,
                "--allowedTools"] + BOARD_CLI_PERMISSIONS

    def build_start_args(self, session_id: str, system_file: Path) -> List[str]:
        args = [self.binary, "-p", "--session-id", session_id] + self._common()
        if self.model:
            args += ["--model", self.model]
        return args + ["--append-system-prompt-file", str(system_file)]

    def build_resume_args(self, session_id: str, system_file: Path) -> List[str]:
        # системный промпт не хранится в сессии: его передают на каждый ход
        return [self.binary, "-p", "--resume", session_id] + self._common() + [
            "--append-system-prompt-file", str(system_file)]

    def _write_system_file(self, log: Path) -> Path:
        system_file = Path(str(log) + ".system.md")
        system_file.parent.mkdir(parents=True, exist_ok=True)
        system_file.write_text(self.system_prompt, encoding="utf-8")
        return system_file

    def start_turn(self, cwd, prompt, session_id, log, env):
        session_id = session_id or self.new_session_id()
        try:
            system_file = self._write_system_file(log)
        except OSError as e:
            return FailedProc("не удалось записать системный промпт: %s" % e, session_id)
        return self._spawn(self.build_start_args(session_id, system_file), cwd, env, session_id, prompt)

    def resume_turn(self, cwd, session_id, message, log, env):
        try:
            system_file = self._write_system_file(log)
        except OSError as e:
            return FailedProc("не удалось записать системный промпт: %s" % e, session_id)
        return self._spawn(self.build_resume_args(session_id, system_file), cwd, env, session_id, message)

    def terminal_command(self, cwd, session_id):
        return "cd %s && claude --resume %s" % (shlex.quote(str(cwd)), shlex.quote(session_id))

    def _on_event(self, event, state):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
            if state["session_id"] and state["session_id"] != event["session_id"]:
                state["note"] = "id сессии в потоке (%s) отличается от переданного (%s)" % (event["session_id"], state["session_id"])
            else:
                state["session_id"] = event["session_id"]
        elif kind == "result":
            if event.get("is_error"):
                state["error"] = str(event.get("result") or event.get("error") or "ошибка агента")
            else:
                state["error"] = ""


class CodexHarness(Harness):
    binary = "codex"

    def __init__(self, sandbox: str = "workspace-write", model: Optional[str] = None):
        self.sandbox = sandbox
        self.model = model

    def _exec_args(self) -> List[str]:
        # сеть нужна, чтобы агент достучался до локальной доски
        args = [self.binary, "exec", "--json", "--sandbox", self.sandbox,
                "-c", "sandbox_workspace_write.network_access=true"]
        if self.model:
            args += ["--model", self.model]
        return args

    def build_start_args(self) -> List[str]:
        return self._exec_args() + ["-"]

    def build_resume_args(self, session_id: str) -> List[str]:
        return self._exec_args() + ["resume", session_id, "-"]

    def start_turn(self, cwd, prompt, session_id, log, env):
        return self._spawn(self.build_start_args(), cwd, env, None, prompt)

    def resume_turn(self, cwd, session_id, message, log, env):
        return self._spawn(self.build_resume_args(session_id), cwd, env, session_id, message)

    def terminal_command(self, cwd, session_id):
        return "cd %s && codex resume %s" % (shlex.quote(str(cwd)), shlex.quote(session_id))

    def _on_event(self, event, state):
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            state["session_id"] = event["thread_id"]
        elif kind == "turn.failed":
            err = event.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            state["error"] = str(msg or event.get("message") or "ход завершился ошибкой")
        elif kind == "error" and not state["error"]:
            state["error"] = str(event.get("message") or "ошибка агента")
        elif kind == "turn.completed":
            state["error"] = ""


class FakeHarness(ClaudeHarness):
    """Скриптуемый агент для тестов: поток в формате Claude, шаги — после маркера FAKE-SCRIPT:."""

    binary = sys.executable

    def __init__(self):
        super().__init__("auto")

    def _spawn_fake(self, session_id: str, text: str, cwd: Path, env: dict, resume: bool = False):
        env = dict(env)
        pkg_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [pkg_root, env.get("PYTHONPATH") or os.environ.get("PYTHONPATH")]))
        args = [sys.executable, "-m", "agent_board.fake_agent", "--session-id", session_id]
        if resume:
            args.append("--resume")
        return self._spawn(args, cwd, env, session_id, text)

    def start_turn(self, cwd, prompt, session_id, log, env):
        return self._spawn_fake(session_id or self.new_session_id(), prompt, cwd, env)

    def resume_turn(self, cwd, session_id, message, log, env):
        return self._spawn_fake(session_id, message, cwd, env, resume=True)


def make_harness(config: dict) -> Harness:
    kind = config.get("type", "claude")
    if kind == "claude":
        return ClaudeHarness(config.get("permission_mode") or "auto", config.get("model"))
    if kind == "codex":
        return CodexHarness(config.get("sandbox") or "workspace-write", config.get("model"))
    if kind == "fake":
        return FakeHarness()
    raise ValueError("Неизвестный харнесс: %s" % kind)
