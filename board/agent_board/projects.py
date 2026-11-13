"""Создание проектов доски и выбор ключа нового проекта."""
from __future__ import annotations

import re
import threading
from typing import Dict, Iterable, Set

from .config import PROJECT_KINDS, ProjectConfig, validate_fields, validate_structure
from .messages import msg
from .settings import default_harness, load_settings, resolve
from .store import ProjectStore, list_projects

PROJECT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,9}$")
_TASK_PREFIX = re.compile(r"^(.+)-\d+$")
# параметр создания → поле настроек; тип агентов задаётся отдельно
OPTION_FIELDS = {"max_agents": "max_agents", "auto_approve": "auto_approve",
                 "project_agent_tasks": "project_agent_tasks"}
OPTIONS = set(OPTION_FIELDS) | {"harness_type"}

# выбор свободного ключа и создание каталога проекта — одно действие
_create_lock = threading.Lock()


def task_prefixes() -> Dict[str, Set[str]]:
    """Префиксы ключей задач каждого проекта (часть до «-<число>», в верхнем регистре)."""
    result: Dict[str, Set[str]] = {}
    for slug in list_projects():
        try:
            keys = ProjectStore(slug).load_tasks()
        except (OSError, ValueError):
            keys = {}
        result[slug] = {m.group(1).upper() for m in map(_TASK_PREFIX.match, keys) if m}
    return result


def unique_project_key(wanted: str, taken: Iterable[str]) -> str:
    """Ключ как есть, если он свободен; иначе с номером: FIX → FIX2, FIX3… Регистр не важен."""
    busy = {t.lower() for t in taken}
    key, n = wanted, 2
    while key.lower() in busy:
        key, n = "%s%d" % (wanted, n), n + 1
    return key


def _taken_keys() -> Set[str]:
    taken = set(list_projects())
    for prefixes in task_prefixes().values():
        taken |= prefixes
    return taken


def new_config(slug: str, title: str, kind: str, options: dict) -> ProjectConfig:
    """Настройки нового проекта: проект без плана сразу на стадии работы; агент, совпадающий с агентом доски,
    не становится собственной настройкой проекта."""
    if not isinstance(options, dict):
        raise ValueError(msg("api.body_not_object"))
    unknown = sorted(set(options) - OPTIONS)
    if unknown:
        raise ValueError(msg("project.unknown_option", name=unknown[0]))
    config = ProjectConfig(slug=slug, title=title, kind=kind)
    if kind == "free":
        config.stage = "work"
    for option, name in OPTION_FIELDS.items():
        if option in options:
            setattr(config, name, options[option])
    if "harness_type" in options:
        harness_type = options["harness_type"]
        if harness_type != load_settings().harness.get("type", "claude"):
            config.harness = default_harness() if harness_type == "claude" else {"type": harness_type}
    validate_fields(config, [OPTION_FIELDS[o] for o in options if o in OPTION_FIELDS]
                    + (["harness"] if "harness_type" in options else []))
    validate_structure(config)
    return config


def create_project(title: str, key: str, kind: str, options: dict) -> ProjectConfig:
    """Создаёт проект; занятый ключ (имя другого проекта или префикс его задач) получает номер."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError(msg("project.title"))
    if not isinstance(key, str) or not PROJECT_KEY.match(key):
        raise ValueError(msg("project.bad_key", key=key))
    if kind not in PROJECT_KINDS:
        raise ValueError(msg("config.kind", kinds=", ".join(PROJECT_KINDS)))
    with _create_lock:
        slug = unique_project_key(key.upper(), _taken_keys()).lower()
        config = new_config(slug, title.strip(), kind, options)
        try:
            ProjectStore(slug).create(config)
        except FileExistsError as e:
            # проект с этим именем успели создать в обход сервера
            raise ValueError(str(e))
    return resolve(config)
