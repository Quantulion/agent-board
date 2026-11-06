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
    decomposition_rules: str = ""
    project_session_id: Optional[str] = None
    links: List[dict] = field(default_factory=list)

    def __post_init__(self):
        self.links = clean_links(self.links)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        legacy = d.pop(LEGACY_SHIP_COMMAND, None)
        config = super().from_dict(d)
        if isinstance(legacy, str) and legacy.strip() and isinstance(config.ship, dict) \
                and not config.ship.get("command"):
            config.ship = dict(config.ship, command=legacy)
        return config


# прежнее место команды отправки: переносится в ship.command при чтении настроек
LEGACY_SHIP_COMMAND = "ship_command"


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
# правила декомпозиции целиком попадают в промпт агента проекта
MAX_DECOMPOSITION_RULES_CHARS = 4000


def _positive_int(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s должен быть целым числом не меньше 1" % name)


def _text(value, name: str, required: bool = False) -> None:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError("%s должен быть %sстрокой" % (name, "непустой " if required else ""))


def _optional_text(value, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("%s должен быть непустой строкой или null" % name)


def _check_decomposition_rules(config: "ProjectConfig") -> None:
    _text(config.decomposition_rules, "decomposition_rules")
    if len(config.decomposition_rules) > MAX_DECOMPOSITION_RULES_CHARS:
        raise ValueError("decomposition_rules длиннее %d символов" % MAX_DECOMPOSITION_RULES_CHARS)


def _git_work_tree(path: str) -> bool:
    import subprocess

    try:
        res = subprocess.run(["git", "-C", path, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def _repos_structure(repos) -> None:
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
        _text(repo.get("path"), "repos[].path", required=True)


def _validate_repos(repos, previous=None) -> None:
    """Проверяет репозитории; каталог и git — только у новых и изменившихся (по имени и пути) относительно previous,
    чтобы удалённый с диска старый репозиторий не мешал править настройки."""
    import os

    _repos_structure(repos)
    known = {(r.get("name"), r.get("path")) for r in previous or [] if isinstance(r, dict)}
    for repo in repos:
        path = repo["path"]
        if not os.path.isabs(path):
            raise ValueError("Путь репозитория %s должен быть абсолютным: %s" % (repo["name"], path))
        _optional_text(repo.get("base"), "repos[].base")
        if (repo["name"], path) in known:
            continue
        if not os.path.isdir(path):
            raise ValueError("Каталог репозитория %s не найден: %s" % (repo["name"], path))
        if not _git_work_tree(path):
            raise ValueError("Каталог репозитория %s — не git-репозиторий: %s" % (repo["name"], path))


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


def _check_review(config: ProjectConfig) -> None:
    review = config.review
    if not isinstance(review, dict) or (review.get("tool") or "crit") not in REVIEW_TOOLS:
        raise ValueError("Неизвестный инструмент ревью: review.tool должен быть одним из: %s" % ", ".join(REVIEW_TOOLS))
    _optional_text(review.get("binary"), "review.binary")


def _check_ship(config: ProjectConfig) -> None:
    from .ship import settings as ship_settings

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


def _check_harness(config: ProjectConfig) -> None:
    harness = config.harness
    if not isinstance(harness, dict) or harness.get("type", "claude") not in HARNESS_TYPES:
        raise ValueError("Неизвестный харнесс: harness.type должен быть одним из: %s" % ", ".join(HARNESS_TYPES))
    for name in ("permission_mode", "model", "sandbox"):
        _optional_text(harness.get(name), "harness." + name)
    tools = harness.get("allowed_tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools)):
        raise ValueError("harness.allowed_tools должен быть списком строк")


def _check_tracker(config: ProjectConfig) -> None:
    from .tracker import validate_config as validate_tracker

    validate_tracker(config.tracker)


def _check_chains(config: ProjectConfig) -> None:
    if not isinstance(config.chains, bool):
        raise ValueError("chains должен быть true или false")


# полные проверки каждого поля, которое задаёт пользователь (repos проверяются отдельно — с учётом прежних)
FIELD_CHECKS = {
    "title": lambda c: _text(c.title, "title", required=True),
    "chains": _check_chains,
    "max_agents": lambda c: _positive_int(c.max_agents, "max_agents"),
    "global_max_agents": lambda c: _positive_int(c.global_max_agents, "global_max_agents"),
    "language": lambda c: _text(c.language, "language", required=True),
    "agent_rules": lambda c: _text(c.agent_rules, "agent_rules"),
    "project_brief": lambda c: _text(c.project_brief, "project_brief"),
    "decomposition_rules": _check_decomposition_rules,
    "tracker": _check_tracker,
    "review": _check_review,
    "ship": _check_ship,
    "resources": lambda c: _validate_resources(c.resources),
    "harness": _check_harness,
    "branch_template": lambda c: _validate_branch_template(c.branch_template),
    "links": lambda c: _validate_links(c.links),
}


def check_field_names(names) -> None:
    """Имена полей, которые пользователь может задать в настройках; прочие — ValueError."""
    from dataclasses import fields as dataclass_fields

    known = {f.name for f in dataclass_fields(ProjectConfig)}
    for name in names:
        if name in NOT_SETTABLE:
            raise ValueError("Поле %s нельзя менять в настройках: %s" % (name, NOT_SETTABLE[name]))
        if name not in known:
            raise ValueError("Неизвестное поле конфига: %s" % name)


def validate_fields(config: ProjectConfig, names, previous_repos=None) -> None:
    """Полные проверки заданных полей; у repos каталог и git проверяются только для новых и изменившихся."""
    for name in names:
        if name == "repos":
            _validate_repos(config.repos, previous_repos)
        elif name in FIELD_CHECKS:
            FIELD_CHECKS[name](config)


def validate_structure(config: ProjectConfig) -> None:
    """Проверки формы всех настроек: типы, перечисления и согласованность полей — без проверок, которые могут
    устареть сами по себе (диск, git, правила шаблонов)."""
    if not isinstance(config.title, str):
        raise ValueError("title должен быть строкой")
    _check_chains(config)
    _positive_int(config.max_agents, "max_agents")
    _positive_int(config.global_max_agents, "global_max_agents")
    for name in ("language", "agent_rules", "project_brief", "decomposition_rules", "branch_template"):
        _text(getattr(config, name), name)
    _repos_structure(config.repos)
    tracker = config.tracker
    if not isinstance(tracker, dict) or not isinstance(tracker.get("type", "none"), str):
        raise ValueError("tracker должен быть объектом с полем type")
    review = config.review
    if not isinstance(review, dict) or (review.get("tool") or "crit") not in REVIEW_TOOLS:
        raise ValueError("Неизвестный инструмент ревью: review.tool должен быть одним из: %s" % ", ".join(REVIEW_TOOLS))
    if not isinstance(config.ship, dict):
        raise ValueError("ship должен быть объектом {command, timeout}")
    if not isinstance(config.resources, list):
        raise ValueError("resources должен быть списком ресурсов")
    harness = config.harness
    if not isinstance(harness, dict) or harness.get("type", "claude") not in HARNESS_TYPES:
        raise ValueError("Неизвестный харнесс: harness.type должен быть одним из: %s" % ", ".join(HARNESS_TYPES))
    if not isinstance(config.links, list):
        raise ValueError("links должен быть списком ссылок")


def validate_config(config: ProjectConfig) -> None:
    """Все проверки настроек проекта целиком; ошибка — ValueError с понятным текстом."""
    validate_fields(config, ["repos"] + list(FIELD_CHECKS))
    validate_structure(config)
