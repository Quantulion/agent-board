"""Папки на диске пользователя: подсказка путей и проверка git-репозиториев."""
from __future__ import annotations

import os
import subprocess
from typing import List, Optional

GIT_TIMEOUT = 10
DEFAULT_LIMIT = 20


def _git(path: str, *args: str) -> Optional[str]:
    """Вывод git-команды в папке или None, если она не удалась."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        res = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def default_branch(path: str) -> str:
    """Ветка, на которую смотрит origin, а без неё — текущая; пустая строка, если ветку не определить."""
    remote = _git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote:
        return remote.split("/", 1)[1] if "/" in remote else remote
    return _git(path, "symbolic-ref", "--quiet", "--short", "HEAD") or ""


def work_tree_root(path: str) -> Optional[str]:
    """Настоящий путь корня git-репозитория, в котором лежит папка, или None, если это не git."""
    if _git(path, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    top = _git(path, "rev-parse", "--show-toplevel")
    return os.path.realpath(top) if top else None


def complete(prefix: str, limit: int = DEFAULT_LIMIT) -> List[dict]:
    """Подпапки, подходящие под введённый путь: всё до последнего / — родительская папка, остальное — начало имени.

    Путь показывается так, как его ввёл пользователь (с ~), скрытые папки — только если начало имени с точки."""
    if prefix == "~":
        prefix = "~/"
    if not prefix.startswith(("/", "~/")):
        return []
    typed_parent, start = prefix.rsplit("/", 1)
    typed_parent += "/"
    parent = os.path.abspath(os.path.expanduser(typed_parent))
    try:
        names = sorted(os.listdir(parent), key=str.lower)
    except OSError:
        return []
    items: List[dict] = []
    for name in names:
        if len(items) >= limit:
            break
        if name.startswith(".") and not start.startswith("."):
            continue
        if not name.lower().startswith(start.lower()):
            continue
        full = os.path.join(parent, name)
        if not os.path.isdir(full):
            continue
        git = os.path.exists(os.path.join(full, ".git"))
        items.append({"path": typed_parent + name, "abs": full, "git": git,
                      "branch": (default_branch(full) or None) if git else None})
    return items
