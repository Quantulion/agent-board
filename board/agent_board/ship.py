from __future__ import annotations

import os
import shlex
import signal
import string
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .model import Status, Task

DEFAULT_TIMEOUT = 600.0
OUTPUT_TAIL_BYTES = 4096
# вывод пишется в лог построчно; строка длиннее лимита пишется частями, а конец, где может начинаться секрет,
# придерживается до следующей части
COPY_LINE_LIMIT = 64 * 1024
COPY_KEEP = 16 * 1024
COPY_JOIN_SECONDS = 5.0
PLACEHOLDERS = ("key", "title", "branch", "base", "worktree", "repo", "tracker_key", "tracker_url")
# зависимость уже влита или отменена: отправлять её не нужно
SHIPPED = (Status.SENT, Status.DONE, Status.CANCELLED)


def settings(config) -> Tuple[str, float]:
    """Команда отправки и таймаут из настроек проекта; без команды отправка недоступна."""
    ship = config.ship if isinstance(config.ship, dict) else {}
    command = ship.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("В проекте не задана команда отправки в ревью (ship.command в настройках)")
    timeout = ship.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("ship.timeout должен быть положительным числом секунд")
    validate_template(command)
    return command, float(timeout)


def on_approve(config) -> bool:
    """Отправлять ли задачу в ревью сразу после одобрения."""
    ship = config.ship if isinstance(config.ship, dict) else {}
    command = ship.get("command")
    return ship.get("on_approve") is True and isinstance(command, str) and bool(command.strip())


def tracker_env(task: Task) -> Dict[str, str]:
    """Привязка задачи к трекеру для команды отправки; без привязки — пустые строки."""
    return {"AGENT_BOARD_TRACKER_KEY": task.tracker_key or "", "AGENT_BOARD_TRACKER_URL": task.tracker_url or ""}


QUOTES_TEXT = "Плейсхолдер внутри кавычек: не берите плейсхолдеры в кавычки — значения уже экранированы"
ANSI_C_TEXT = "Кавычки $'…' в команде отправки не поддерживаются"
HEREDOC_TEXT = "Встроенный текст << (heredoc) в команде отправки не поддерживается: вынесите его в скрипт"


def check_quotes(command: str) -> None:
    """Плейсхолдер внутри '…' или "…" отвергается: подставленное значение вышло бы из экранирования.

    Кавычки $'…' и встроенный текст << отвергаются целиком: в них иные правила экранирования,
    и проверка их не разбирает.
    """
    quote = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote is None:
            if c == "\\":
                i += 1
            elif c == "$" and command[i + 1:i + 2] == "'":
                raise ValueError(ANSI_C_TEXT)
            elif c == "<" and command[i + 1:i + 2] == "<":
                raise ValueError(HEREDOC_TEXT)
            elif c in "'\"":
                quote = c
        elif c == "\\" and quote == '"':
            i += 1
        elif c == quote:
            quote = None
        elif quote is not None and c == "{":
            if command[i + 1:i + 2] == "{":
                i += 1
            elif "}" in command[i:]:
                raise ValueError(QUOTES_TEXT)
        i += 1


def validate_template(command: str) -> None:
    """Шаблон может содержать только известные плейсхолдеры без индексов и форматирования."""
    check_quotes(command)
    try:
        parsed = list(string.Formatter().parse(command))
    except ValueError as e:
        raise ValueError("Некорректный шаблон команды отправки: %s" % e)
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if name not in PLACEHOLDERS or spec or conversion:
            raise ValueError("Неизвестный плейсхолдер в команде отправки: {%s}. Допустимы: %s" % (
                name, ", ".join("{%s}" % p for p in PLACEHOLDERS)))


def render(command: str, values: Dict[str, str]) -> str:
    """Подставляет значения, экранированные для shell."""
    return command.format_map({k: shlex.quote(str(v)) for k, v in values.items()})


def order(keys: List[str], tasks: Dict[str, Task]) -> List[str]:
    """Топологический порядок запроса: зависимости раньше зависимых, при равенстве — по ключу."""
    wanted = set(keys)
    result: List[str] = []
    done: set = set()

    def visit(key: str) -> None:
        if key in done:
            return
        done.add(key)
        for dep in sorted(tasks[key].deps):
            if dep in wanted:
                visit(dep)
        result.append(key)

    for key in sorted(wanted):
        visit(key)
    return result


def check_request(keys: List[str], tasks: Dict[str, Task]) -> None:
    """Все задачи одобрены, а незавершённые зависимости не из запроса уже отправлены."""
    wanted = set(keys)
    for key in keys:
        task = tasks[key]
        if task.status != Status.APPROVED:
            raise ValueError("Отправить можно только одобренную задачу: %s (сейчас %s)" % (key, task.status.value))
    for key in keys:
        for dep in tasks[key].deps:
            dep_task = tasks.get(dep)
            if dep in wanted or dep_task is None or dep_task.status in SHIPPED:
                continue
            if dep_task.status == Status.APPROVED:
                raise ValueError("%s: сначала отправьте %s" % (key, dep))
            raise ValueError("%s: зависимость %s ещё не одобрена (%s)" % (key, dep, dep_task.status.value))


def is_shipping(task: Task) -> bool:
    return isinstance(task.ship, dict) and task.ship.get("state") in ("queued", "running")


def check_auto(key: str, tasks: Dict[str, Task]) -> None:
    """Отправка после одобрения: задача одобрена, а каждая незавершённая зависимость уже отправлена или
    стоит в очереди отправки — тогда задача уйдёт следом за ней."""
    task = tasks[key]
    if task.status != Status.APPROVED:
        raise ValueError("задача уже не одобрена (%s)" % task.status.value)
    if is_shipping(task):
        raise ValueError("задача уже отправляется")
    for dep in sorted(task.deps):
        dep_task = tasks.get(dep)
        if dep_task is None or dep_task.status in SHIPPED:
            continue
        if dep_task.status != Status.APPROVED:
            raise ValueError("зависимость %s ещё не одобрена (%s)" % (dep, dep_task.status.value))
        if not is_shipping(dep_task):
            raise ValueError("зависимость %s ещё не отправлена — отправьте её, затем эту задачу" % dep)


def tail_of(path: Path, limit: int = OUTPUT_TAIL_BYTES) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        text = encoded[-limit:].decode("utf-8", errors="ignore")
    return text


def copy_output(src, log: Path, scrub: Callable[[str], str]) -> None:
    """Переписывает вывод команды в лог, маскируя секреты (scrub) до записи на диск."""
    def clean(data: bytes) -> bytes:
        return scrub(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")

    pending = b""
    with log.open("ab") as out:
        while True:
            try:
                chunk = src.read1(COPY_LINE_LIMIT)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            pending += chunk
            cut = pending.rfind(b"\n") + 1
            if cut:
                out.write(clean(pending[:cut]))
                pending = pending[cut:]
            elif len(pending) > COPY_LINE_LIMIT:
                cleaned = clean(pending)
                out.write(cleaned[:-COPY_KEEP])
                pending = cleaned[-COPY_KEEP:]
            out.flush()
        out.write(clean(pending))


def start(rendered: str, cwd: Path, log: Path, task_key: str, env: Optional[dict] = None,
          scrub: Optional[Callable[[str], str]] = None, extra_env: Optional[Dict[str, str]] = None) -> "subprocess.Popen":
    """Запускает команду в своей группе процессов, вывод пишется в лог (с scrub — через маскирование секретов;
    поток записи — proc.copier)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    full_env = dict(os.environ if env is None else env, AGENT_BOARD_TASK=task_key)
    full_env.update(extra_env or {})
    if scrub is None:
        with log.open("wb") as out:
            proc = subprocess.Popen(["bash", "-lc", rendered], cwd=str(cwd), env=full_env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        proc.copier = None
        return proc
    log.write_bytes(b"")
    proc = subprocess.Popen(["bash", "-lc", rendered], cwd=str(cwd), env=full_env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    stdout = proc.stdout

    def copy() -> None:
        try:
            copy_output(stdout, log, scrub)
        finally:
            stdout.close()

    proc.copier = threading.Thread(target=copy, name="board-ship-log-%s" % task_key, daemon=True)
    try:
        proc.copier.start()
    except RuntimeError as e:
        # без чтения вывода команда встанет на заполненном канале: останавливаем, пока лидер не собран
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        stdout.close()
        raise OSError("не удалось запустить запись вывода: %s" % e)
    return proc


def finish_output(proc) -> None:
    """Дожидается записи вывода в лог (не дольше COPY_JOIN_SECONDS: вывод может держать сбежавший потомок)."""
    copier = getattr(proc, "copier", None)
    if copier is not None:
        copier.join(COPY_JOIN_SECONDS)

