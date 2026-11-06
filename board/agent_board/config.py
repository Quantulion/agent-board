from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import string
from typing import List, Optional

from .model import _Serializable


@dataclass
class ProjectConfig(_Serializable):
    slug: str
    title: str
    chains: bool = True
    max_agents: int = 3
    language: str = "ru"
    repos: List[dict] = field(default_factory=list)
    tracker: dict = field(default_factory=lambda: {"type": "none"})
    review: dict = field(default_factory=lambda: {"tool": "crit", "binary": "crit"})
    ship: dict = field(default_factory=lambda: {"command": None, "timeout": 600})
    resources: List[dict] = field(default_factory=list)
    agent_rules: str = ""
    stage: str = "decomposition"
    harness: dict = field(default_factory=lambda: {"type": "claude", "permission_mode": "auto"})
    branch_template: str = "{key}"
    global_max_agents: int = 6
    project_brief: str = ""
    project_session_id: Optional[str] = None
    links: List[dict] = field(default_factory=list)

    def __post_init__(self):
        self.links = clean_links(self.links)


_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HTTP = re.compile(r"^https?://", re.IGNORECASE)
_RESOURCE = re.compile(r"^[A-Za-z0-9._-]+$")


def clean_links(links) -> List[dict]:
    """Оставляет ссылки с http(s) или обычным путём; прочие схемы (javascript:, data:, file:) отбрасываются."""
    result = []
    for item in links if isinstance(links, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("href"), str):
            continue
        href = item["href"].strip()
        if href and (_HTTP.match(href) or not _SCHEME.match(href)):
            result.append({"title": item["title"], "href": href})
    return result


# настройки, которые ведёт не пользователь: имя проекта, стадия (своя команда) и сессия агента проекта (раннер)
NOT_SETTABLE = {"slug": "имя проекта не меняется", "stage": "стадию меняет команда stage",
                "project_session_id": "сессию агента проекта ведёт раннер"}
HARNESS_TYPES = ("claude", "codex", "fake")
REVIEW_TOOLS = ("crit", "none")


def _positive_int(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s должен быть целым числом не меньше 1" % name)


def _text(value, name: str, required: bool = False) -> None:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError("%s должен быть %sстрокой" % (name, "непустой " if required else ""))


def _optional_text(value, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("%s должен быть непустой строкой или null" % name)


def _git_work_tree(path: str) -> bool:
    import subprocess

    try:
        res = subprocess.run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def _validate_repos(repos) -> None:
    import os

    if not isinstance(repos, list):
        raise ValueError("repos должен быть списком репозиториев")
    names = set()
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError("Репозиторий в repos должен быть объектом {name, path, base}")
        _text(repo.get("name"), "repos[].name", required=True)
        if repo["name"] in names:
            raise ValueError("Репозиторий %s указан дважды" % repo["name"])
        names.add(repo["name"])
        path = repo.get("path")
        _text(path, "repos[].path", required=True)
        if not os.path.isabs(path):
            raise ValueError("Путь репозитория %s должен быть абсолютным: %s" % (repo["name"], path))
        if not os.path.isdir(path):
            raise ValueError("Каталог репозитория %s не найден: %s" % (repo["name"], path))
        if not _git_work_tree(path):
            raise ValueError("Каталог репозитория %s — не git-репозиторий: %s" % (repo["name"], path))
        _optional_text(repo.get("base"), "repos[].base")


def _validate_branch_template(template) -> None:
    _text(template, "branch_template", required=True)
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise ValueError("Некорректный branch_template: %s" % e)
    names = [name for _, name, _, _ in parsed if name is not None]
    if any(name != "key" for name in names) or any(spec or conv for _, name, spec, conv in parsed if name):
        raise ValueError("В branch_template допустим только плейсхолдер {key}")
    if "key" not in names:
        raise ValueError("branch_template должен содержать {key}: у каждой задачи своя ветка")


def _validate_resources(resources) -> None:
    if not isinstance(resources, list):
        raise ValueError("resources должен быть списком ресурсов")
    names = set()
    for item in resources:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _RESOURCE.match(item["name"]):
            raise ValueError("Ресурс в resources должен быть объектом {name, description} с именем из латиницы, цифр, "
                             "«-», «_» и «.»")
        if item["name"] in names:
            raise ValueError("Ресурс %s указан дважды" % item["name"])
        names.add(item["name"])
        if item.get("description") is not None:
            _text(item["description"], "resources[].description")


def _validate_links(links) -> None:
    if not isinstance(links, list):
        raise ValueError("links должен быть списком ссылок")
    for item in links:
        if len(clean_links([item])) != 1:
            raise ValueError("Недопустимая ссылка в links: %s — нужны title и href (http(s) или путь к файлу)"
                             % json.dumps(item, ensure_ascii=False)[:200])


def validate_config(config: ProjectConfig) -> None:
    """Все проверки настроек проекта перед сохранением; ошибка — ValueError с понятным текстом."""
    from .ship import settings as ship_settings
    from .tracker import validate_config as validate_tracker

    _text(config.title, "title", required=True)
    if not isinstance(config.chains, bool):
        raise ValueError("chains должен быть true или false")
    _positive_int(config.max_agents, "max_agents")
    _positive_int(config.global_max_agents, "global_max_agents")
    _text(config.language, "language", required=True)
    _text(config.agent_rules, "agent_rules")
    _text(config.project_brief, "project_brief")
    _validate_repos(config.repos)
    validate_tracker(config.tracker)
    review = config.review
    if not isinstance(review, dict) or (review.get("tool") or "crit") not in REVIEW_TOOLS:
        raise ValueError("Неизвестный инструмент ревью: review.tool должен быть одним из: %s" % ", ".join(REVIEW_TOOLS))
    _optional_text(review.get("binary"), "review.binary")
    ship = config.ship
    if not isinstance(ship, dict):
        raise ValueError("ship должен быть объектом {command, timeout}")
    if ship.get("command") is not None:
        _text(ship["command"], "ship.command")
    if isinstance(ship.get("command"), str) and ship["command"].strip():
        ship_settings(config)
    else:
        timeout = ship.get("timeout", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("ship.timeout должен быть положительным числом секунд")
    _validate_resources(config.resources)
    harness = config.harness
    if not isinstance(harness, dict) or harness.get("type", "claude") not in HARNESS_TYPES:
        raise ValueError("Неизвестный харнесс: harness.type должен быть одним из: %s" % ", ".join(HARNESS_TYPES))
    for name in ("permission_mode", "model", "sandbox"):
        _optional_text(harness.get(name), "harness." + name)
    tools = harness.get("allowed_tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools)):
        raise ValueError("harness.allowed_tools должен быть списком строк")
    _validate_branch_template(config.branch_template)
    _validate_links(config.links)
