from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List

MARKER = "FAKE-SCRIPT:"
# сколько шаг wait-file ждёт файл, прежде чем завершить ход ошибкой
WAIT_FILE_TIMEOUT = 60.0


def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _fail(session_id: str, message: str) -> int:
    _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": message, "session_id": session_id})
    return 1


def _run_cli(argv: List[str]) -> str:
    from . import cli

    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(argv)
    return "" if code == 0 else (err.getvalue().strip() or "команда %s завершилась с кодом %d" % (argv[0], code))


def _wait_file(path: Path) -> bool:
    deadline = time.monotonic() + WAIT_FILE_TIMEOUT
    while not path.exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def run(session_id: str, text: str, resumed: bool = False) -> int:
    _emit({"type": "system", "subtype": "init", "session_id": session_id})
    steps = []
    if MARKER in text:
        # хвост после списка шагов (склеенные сообщения пробуждений) игнорируется
        steps, _ = json.JSONDecoder().raw_decode(text.split(MARKER, 1)[1].lstrip())
    for step in steps:
        if "cli" in step or "lock-run" in step:
            argv = step["cli"] if "cli" in step else ["lock-run"] + step["lock-run"]
            error = _run_cli(list(argv))
            if error:
                return _fail(session_id, error)
        elif "pingpong" in step:
            # переписка агентов: агент задачи пишет агенту проекта, тот — обратно в задачу, тем же сценарием
            peer = step["pingpong"] if os.environ.get("AGENT_BOARD_ACTOR") == "project_agent" else "_project"
            error = _run_cli(["say", peer, "--text", MARKER + " " + json.dumps(steps, ensure_ascii=False)])
            if error:
                return _fail(session_id, error)
        elif "write" in step:
            target = Path(step["write"]["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(step["write"]["text"], encoding="utf-8")
        elif "background" in step:
            # фоновая команда хода (как запущенные агентом тесты): pid пишется в файл
            proc = subprocess.Popen(step["background"]["argv"])
            target = Path(step["background"]["pid_file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(proc.pid), encoding="utf-8")
        elif "wait-file" in step:
            # ход держится, пока тест не создаст файл: так тест сам решает, когда агенту закончить
            if not _wait_file(Path(step["wait-file"])):
                return _fail(session_id, "не дождались файла %s" % step["wait-file"])
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "fail-if-resumed" in step:
            if resumed:
                return _fail(session_id, str(step["fail-if-resumed"]))
        elif "fail" in step:
            return _fail(session_id, str(step["fail"]))
        else:
            return _fail(session_id, "неизвестный шаг: %s" % json.dumps(step, ensure_ascii=False))
    _emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": session_id})
    return 0


def main(argv: List[str] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    session_id = argv[argv.index("--session-id") + 1]
    text = sys.stdin.read()
    return run(session_id, text, "--resume" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
