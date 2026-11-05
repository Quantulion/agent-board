from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


class MissingBinaryProc:
    """Заглушка вместо процесса, если бинарь харнесса не найден."""

    stdout = None
    returncode = 127
    pid = None

    def __init__(self, binary: str):
        self.error = "%s не найден в PATH" % binary

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

    def _spawn(self, args: List[str], cwd: Path, env: dict, session_hint: Optional[str]):
        full_env = dict(os.environ)
        full_env.update(env)
        if shutil.which(args[0], path=full_env.get("PATH")) is None:
            return MissingBinaryProc(args[0])
        proc = subprocess.Popen(
            args, cwd=str(cwd), env=full_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
        proc.session_hint = session_hint
        return proc

    def finish(self, proc, log: Path) -> TurnResult:
        if isinstance(proc, MissingBinaryProc):
            return TurnResult(session_id=None, ok=False, error=proc.error, exit_code=proc.returncode)
        state = {"session_id": getattr(proc, "session_hint", None), "error": ""}
        tail = ""
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
        error = state["error"]
        if not error and code != 0:
            error = tail.strip() or "процесс завершился с кодом %d" % code
        return TurnResult(session_id=state["session_id"], ok=not error and code == 0, error=error, exit_code=code)

    def _on_event(self, event: dict, state: dict) -> None:
        raise NotImplementedError


def _parse(line: str) -> Optional[dict]:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


class ClaudeHarness(Harness):
    binary = "claude"

    def __init__(self, permission_mode: str, model: Optional[str] = None, system_prompt: str = DEFAULT_SYSTEM_PROMPT):
        self.permission_mode = permission_mode
        self.model = model
        self.system_prompt = system_prompt

    def new_session_id(self) -> Optional[str]:
        return str(uuid.uuid4())

    def _common(self) -> List[str]:
        return ["--output-format", "stream-json", "--verbose", "--permission-mode", self.permission_mode]

    def build_start_args(self, prompt: str, session_id: str) -> List[str]:
        args = [self.binary, "-p", "--session-id", session_id] + self._common()
        if self.model:
            args += ["--model", self.model]
        return args + ["--append-system-prompt", self.system_prompt, prompt]

    def build_resume_args(self, session_id: str, message: str) -> List[str]:
        return [self.binary, "-p", "--resume", session_id] + self._common() + [message]

    def start_turn(self, cwd, prompt, session_id, log, env):
        session_id = session_id or self.new_session_id()
        return self._spawn(self.build_start_args(prompt, session_id), cwd, env, session_id)

    def resume_turn(self, cwd, session_id, message, log, env):
        return self._spawn(self.build_resume_args(session_id, message), cwd, env, session_id)

    def terminal_command(self, cwd, session_id):
        return "cd %s && claude --resume %s" % (cwd, session_id)

    def _on_event(self, event, state):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and event.get("session_id"):
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

    def build_start_args(self, cwd: Path, prompt: str) -> List[str]:
        args = [self.binary, "exec", "--json", "-C", str(cwd), "--sandbox", self.sandbox]
        if self.model:
            args += ["--model", self.model]
        return args + [prompt]

    def build_resume_args(self, cwd: Path, session_id: str, message: str) -> List[str]:
        return [self.binary, "exec", "--json", "-C", str(cwd), "resume", session_id, message]

    def start_turn(self, cwd, prompt, session_id, log, env):
        return self._spawn(self.build_start_args(cwd, prompt), cwd, env, None)

    def resume_turn(self, cwd, session_id, message, log, env):
        return self._spawn(self.build_resume_args(cwd, session_id, message), cwd, env, session_id)

    def terminal_command(self, cwd, session_id):
        return "cd %s && codex resume %s" % (cwd, session_id)

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


class FakeHarness(ClaudeHarness):
    """Скриптуемый агент для тестов: поток в формате Claude, шаги — после маркера FAKE-SCRIPT:."""

    binary = sys.executable

    def __init__(self):
        super().__init__("auto")

    def _fake_args(self, session_id: str, text: str) -> List[str]:
        return [sys.executable, "-m", "agent_board.fake_agent", "--session-id", session_id, "--", text]

    def _spawn_fake(self, session_id: str, text: str, cwd: Path, env: dict):
        env = dict(env)
        pkg_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [pkg_root, env.get("PYTHONPATH") or os.environ.get("PYTHONPATH")]))
        return self._spawn(self._fake_args(session_id, text), cwd, env, session_id)

    def start_turn(self, cwd, prompt, session_id, log, env):
        return self._spawn_fake(session_id or self.new_session_id(), prompt, cwd, env)

    def resume_turn(self, cwd, session_id, message, log, env):
        return self._spawn_fake(session_id, message, cwd, env)


def make_harness(config: dict) -> Harness:
    kind = config.get("type", "claude")
    if kind == "claude":
        return ClaudeHarness(config.get("permission_mode") or "auto", config.get("model"))
    if kind == "codex":
        return CodexHarness(config.get("sandbox") or "workspace-write", config.get("model"))
    if kind == "fake":
        return FakeHarness()
    raise ValueError("Неизвестный харнесс: %s" % kind)
