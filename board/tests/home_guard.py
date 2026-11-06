from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional

ENV_PREFIX = "AGENT_BOARD_"


def clear_board_env(environ=None) -> None:
    """Убирает из окружения все настройки доски, унаследованные от сессии агента."""
    environ = os.environ if environ is None else environ
    for name in [n for n in environ if n.startswith(ENV_PREFIX)]:
        del environ[name]


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot(root: Path) -> Dict[str, Optional[str]]:
    """То, что работающая доска сама не меняет: состав проектов, server.json, token и настройки проектов.

    Журналы, задачи и события пишут живые агенты — их не учитываем."""
    result = {name: _digest(root / name) for name in ("server.json", "token")}
    projects = root / "projects"
    if projects.is_dir():
        for project in projects.iterdir():
            if project.is_dir():
                # пустая строка отличает новый проект без настроек от отсутствующего
                result["projects/%s/project.json" % project.name] = _digest(project / "project.json") or ""
    return result


def changes(before: Dict[str, Optional[str]], after: Dict[str, Optional[str]]) -> List[str]:
    """Пути, которые появились, исчезли или изменились между двумя снимками."""
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
