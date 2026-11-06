from __future__ import annotations

import os
import shlex
import string
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .model import Status, Task

DEFAULT_TIMEOUT = 600.0
OUTPUT_TAIL_BYTES = 4096
PLACEHOLDERS = ("key", "title", "branch", "base", "worktree", "repo")
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


QUOTES_TEXT = "Плейсхолдер внутри кавычек: не берите плейсхолдеры в кавычки — значения уже экранированы"


def check_quotes(command: str) -> None:
    """Плейсхолдер внутри '…' или "…" отвергается: подставленное значение вышло бы из экранирования."""
    quote = None
    i = 0
    while i < len(command):
        c = command[i]
        if quote is None:
            if c == "\\":
                i += 1
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


def start(rendered: str, cwd: Path, log: Path, task_key: str, env: Optional[dict] = None) -> "subprocess.Popen":
    """Запускает команду в своей группе процессов, вывод пишется в лог."""
    log.parent.mkdir(parents=True, exist_ok=True)
    full_env = dict(os.environ if env is None else env, AGENT_BOARD_TASK=task_key)
    with log.open("wb") as out:
        return subprocess.Popen(["bash", "-lc", rendered], cwd=str(cwd), env=full_env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)

