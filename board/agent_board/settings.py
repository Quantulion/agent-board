"""Настройки всей доски: язык, общий лимит агентов и значения по умолчанию для проектов."""
from __future__ import annotations

import copy
import json
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import (
    BOARD_FIELDS, INHERITED, ProjectConfig, check_global_max_agents, check_harness, check_language, check_review,
)
from .messages import msg
from .model import _Serializable
from .store import atomic_write_json, board_home, list_projects

SETTINGS_FILE = "settings.json"
DEFAULT_LANGUAGE = "ru"
DEFAULT_GLOBAL_MAX_AGENTS = 6


def default_harness() -> dict:
    return {"type": "claude", "permission_mode": "auto"}


def default_review() -> dict:
    return {"tool": "crit", "binary": "crit"}


@dataclass
class BoardSettings(_Serializable):
    language: str = DEFAULT_LANGUAGE
    global_max_agents: int = DEFAULT_GLOBAL_MAX_AGENTS
    harness: dict = field(default_factory=default_harness)
    review: dict = field(default_factory=default_review)


FIELDS = BOARD_FIELDS + INHERITED
CHECKS = {
    "language": check_language,
    "global_max_agents": check_global_max_agents,
    "harness": check_harness,
    "review": check_review,
}
# значения, которые проекты прежнего формата получали, если поле в их настройках не было записано
_LEGACY_DEFAULTS = {"language": DEFAULT_LANGUAGE, "global_max_agents": DEFAULT_GLOBAL_MAX_AGENTS,
                    "harness": default_harness(), "review": default_review()}

_lock = threading.RLock()
_cache: Dict[str, Any] = {}


def settings_path() -> Path:
    return board_home() / SETTINGS_FILE


def _valid(name: str, value) -> bool:
    try:
        CHECKS[name](value)
    except ValueError:
        return False
    return True


def _from_raw(raw) -> BoardSettings:
    """Настройки из файла; испорченное вручную поле заменяется значением по умолчанию."""
    settings = BoardSettings()
    if isinstance(raw, dict):
        for name in FIELDS:
            if name in raw and _valid(name, raw[name]):
                setattr(settings, name, raw[name])
    return settings


def load_settings() -> BoardSettings:
    """Действующие настройки доски; при первом обращении к доске без файла настроек они переносятся из проектов."""
    path = settings_path()
    if not path.is_file():
        migrate()
    try:
        st = path.stat()
    except OSError:
        return BoardSettings()
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        if _cache.get("stamp") == stamp:
            return copy.deepcopy(_cache["settings"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return BoardSettings()
    settings = _from_raw(raw)
    with _lock:
        _cache.update(stamp=stamp, settings=copy.deepcopy(settings))
    return settings


def save_settings(settings: BoardSettings) -> None:
    home = board_home()
    home.mkdir(parents=True, exist_ok=True)
    with _lock:
        atomic_write_json(settings_path(), settings.to_dict())
        _cache.clear()


def update_settings(actor: str, fields: Any, dry_run: bool = False) -> dict:
    """Настройки доски от пользователя; dry_run только проверяет. Возвращает {settings, saved, changed}."""
    if actor != "user":
        raise PermissionError(msg("settings.user_only"))
    if not isinstance(fields, dict):
        raise ValueError(msg("board.fields_not_object"))
    for name in fields:
        if name not in FIELDS:
            raise ValueError(msg("settings.unknown_field", name=name, fields=", ".join(FIELDS)))
    for name, value in fields.items():
        CHECKS[name](value)
    with _lock:
        current = load_settings()
        draft = copy.deepcopy(current)
        for name, value in fields.items():
            setattr(draft, name, copy.deepcopy(value))
        changed = sorted(name for name in fields if getattr(draft, name) != getattr(current, name))
        if changed and not dry_run:
            save_settings(draft)
    return {"settings": draft.to_dict(), "saved": not dry_run, "changed": changed}


# --- наследование настроек проектом ---

def resolve(config: ProjectConfig, settings: Optional[BoardSettings] = None) -> ProjectConfig:
    """Подставляет в незаданные поля проекта значения доски и запоминает, какие поля проект переопределил."""
    settings = settings or load_settings()
    config.overrides = set()
    config.inherited = {}
    for name in INHERITED:
        if getattr(config, name) is None:
            value = copy.deepcopy(getattr(settings, name))
            setattr(config, name, value)
            config.inherited[name] = copy.deepcopy(value)
        else:
            config.overrides.add(name)
    return config


def assign(config: ProjectConfig, name: str, value, settings: Optional[BoardSettings] = None) -> None:
    """Задаёт поле проекта; у наследуемого поля None снимает переопределение и возвращает значение доски."""
    if name not in INHERITED:
        setattr(config, name, value)
        return
    overrides = getattr(config, "overrides", None)
    if overrides is None:
        config.overrides = overrides = set()
        config.inherited = {}
    if value is None:
        value = copy.deepcopy(getattr(settings or load_settings(), name))
        config.inherited[name] = copy.deepcopy(value)
        overrides.discard(name)
    else:
        overrides.add(name)
    setattr(config, name, value)


def overridden(config: ProjectConfig) -> List[str]:
    """Наследуемые поля, у которых проект задал своё значение."""
    inherited = getattr(config, "inherited", None)
    if inherited is None:
        return [name for name in INHERITED if getattr(config, name) is not None]
    overrides = getattr(config, "overrides", set())
    return [name for name in INHERITED
            if name in overrides or name not in inherited or getattr(config, name) != inherited[name]]


def stored(config: ProjectConfig) -> dict:
    """Настройки проекта для записи на диск: унаследованные значения не сохраняются."""
    data = config.to_dict()
    own = set(overridden(config))
    for name in INHERITED:
        if name not in own:
            data[name] = None
    return data


def config_view(config: ProjectConfig, settings: Optional[BoardSettings] = None) -> dict:
    """Действующие настройки проекта для клиентов: со значениями доски и списком переопределённых полей."""
    settings = settings or load_settings()
    data = config.to_dict()
    for name in INHERITED:
        if data.get(name) is None:
            data[name] = copy.deepcopy(getattr(settings, name))
    data["language"] = settings.language
    data["global_max_agents"] = settings.global_max_agents
    data["overrides"] = overridden(config)
    return data


# --- перенос настроек из проектов прежнего формата ---

def _legacy(raw) -> bool:
    """Проект прежнего формата сам хранил язык и общий лимит."""
    return isinstance(raw, dict) and any(name in raw for name in BOARD_FIELDS)


def _most_common(values: Iterable) -> Optional[Any]:
    """Самое частое значение; при равенстве — встретившееся раньше."""
    counter: Counter = Counter()
    first: Dict[str, Any] = {}
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        counter[key] += 1
        first.setdefault(key, value)
    if not counter:
        return None
    return copy.deepcopy(first[counter.most_common(1)[0][0]])


def from_projects(raws: Iterable[dict]) -> BoardSettings:
    """Настройки доски по проектам прежнего формата: наибольший общий лимит, самые частые язык, агент и ревью."""
    raws = list(raws)
    settings = BoardSettings()

    def values(name):
        return [raw.get(name, _LEGACY_DEFAULTS[name]) for raw in raws
                if _valid(name, raw.get(name, _LEGACY_DEFAULTS[name]))]

    limits = values("global_max_agents")
    if limits:
        settings.global_max_agents = max(limits)
    for name in ("language",) + INHERITED:
        value = _most_common(values(name))
        if value is not None:
            setattr(settings, name, value)
    return settings


def migrate() -> bool:
    """Создаёт файл настроек доски, если его нет; значения берутся из проектов прежнего формата.

    У перенесённых проектов язык и общий лимит убираются, а агент и ревью, совпадающие со значениями доски,
    перестают быть переопределениями. Возвращает True, если файл создан."""
    with _lock:
        path = settings_path()
        home = board_home()
        if path.exists() or not home.is_dir():
            return False
        legacy: Dict[Path, dict] = {}
        for slug in list_projects():
            project_file = home / "projects" / slug / "project.json"
            try:
                raw = json.loads(project_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _legacy(raw):
                legacy[project_file] = raw
        settings = from_projects(legacy.values())
        # сначала файл доски: если перенос прервётся, проекты останутся рабочими со своими значениями
        save_settings(settings)
        for project_file, raw in legacy.items():
            updated = {k: v for k, v in raw.items() if k not in BOARD_FIELDS}
            for name in INHERITED:
                value = raw.get(name, _LEGACY_DEFAULTS[name])
                updated[name] = None if value == getattr(settings, name) else value
            atomic_write_json(project_file, updated)
        return True
