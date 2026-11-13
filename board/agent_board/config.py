from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import string
from typing import List, Optional

from .messages import msg
from .model import _Serializable


@dataclass
class ProjectConfig(_Serializable):
    slug: str
    title: str
    chains: bool = True
    auto_approve: bool = False
    max_agents: int = 3
    repos: List[dict] = field(default_factory=list)
    tracker: dict = field(default_factory=lambda: {"type": "none"})
    # None — действует значение из настроек доски
    review: Optional[dict] = None
    ship: dict = field(default_factory=lambda: {"command": None, "timeout": 600, "on_approve": False,
                                                       "on_failure": "stay"})
    resources: List[dict] = field(default_factory=list)
    agent_rules: str = ""
    stage: str = "decomposition"
    harness: Optional[dict] = None
    branch_template: str = "{key}"
    project_brief: str = ""
    decomposition_rules: str = ""
    project_session_id: Optional[str] = None
    links: List[dict] = field(default_factory=list)
    project_agent_status: bool = False
    kind: str = "plan"
    archived: bool = False
    project_agent_tasks: bool = True

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


# настройки, которые ведёт не пользователь: имя и тип проекта, стадия (своя команда) и сессия агента проекта (раннер)
NOT_SETTABLE = {"slug": "config.slug_fixed", "stage": "config.stage_command",
                "project_session_id": "config.session_runner", "kind": "config.kind_fixed"}
# настройки всей доски: в проекте их не задать
BOARD_FIELDS = ("language", "global_max_agents")
# настройки, которые проект берёт у доски, если не задал своё значение
INHERITED = ("harness", "review")
# по плану — задачи из декомпозиции и стадии; без плана — задачи по одной, сразу стадия работы
PROJECT_KINDS = ("plan", "free")
HARNESS_TYPES = ("claude", "codex", "fake")
REVIEW_TOOLS = ("crit", "none")
# что делать с задачей, когда команда отправки упала: оставить одобренной или вернуть агенту
SHIP_ON_FAILURE = ("stay", "return")
# правила декомпозиции целиком попадают в промпт агента проекта
MAX_DECOMPOSITION_RULES_CHARS = 4000


def _positive_int(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(msg("config.positive_int", name=name))


def _text(value, name: str, required: bool = False) -> None:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(msg("config.nonempty_string" if required else "config.string", name=name))


def _optional_text(value, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(msg("config.optional_string", name=name))


def _check_decomposition_rules(config: "ProjectConfig") -> None:
    _text(config.decomposition_rules, "decomposition_rules")
    if len(config.decomposition_rules) > MAX_DECOMPOSITION_RULES_CHARS:
        raise ValueError(msg("config.rules_too_long", max=MAX_DECOMPOSITION_RULES_CHARS))


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
        raise ValueError(msg("config.repos_list"))
    names = set()
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError(msg("config.repo_object"))
        _text(repo.get("name"), "repos[].name", required=True)
        if repo["name"] in names:
            raise ValueError(msg("config.repo_twice", name=repo["name"]))
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
            raise ValueError(msg("config.repo_relative", name=repo["name"], path=path))
        _optional_text(repo.get("base"), "repos[].base")
        if (repo["name"], path) in known:
            continue
        if not os.path.isdir(path):
            raise ValueError(msg("config.repo_missing", name=repo["name"], path=path))
        if not _git_work_tree(path):
            raise ValueError(msg("config.repo_not_git", name=repo["name"], path=path))


def _validate_branch_template(template) -> None:
    _text(template, "branch_template", required=True)
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise ValueError(msg("config.bad_branch_template", error=e))
    names = [name for _, name, _, _ in parsed if name is not None]
    if any(name != "key" for name in names) or any(spec or conv for _, name, spec, conv in parsed if name):
        raise ValueError(msg("config.branch_only_key"))
    if "key" not in names:
        raise ValueError(msg("config.branch_needs_key"))


def _validate_resources(resources) -> None:
    if not isinstance(resources, list):
        raise ValueError(msg("config.resources_list"))
    names = set()
    for item in resources:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _RESOURCE.match(item["name"]):
            raise ValueError(msg("config.resource_object"))
        if item["name"] in names:
            raise ValueError(msg("config.resource_twice", name=item["name"]))
        names.add(item["name"])
        if item.get("description") is not None:
            _text(item["description"], "resources[].description")


def _validate_links(links) -> None:
    if not isinstance(links, list):
        raise ValueError(msg("config.links_list"))
    for item in links:
        if len(clean_links([item])) != 1:
            raise ValueError(msg("config.bad_link", item=json.dumps(item, ensure_ascii=False)[:200]))


def check_review(review) -> None:
    if not isinstance(review, dict) or (review.get("tool") or "crit") not in REVIEW_TOOLS:
        raise ValueError(msg("config.review_tool", tools=", ".join(REVIEW_TOOLS)))
    _optional_text(review.get("binary"), "review.binary")


def _check_review(config: ProjectConfig) -> None:
    if config.review is not None:
        check_review(config.review)


def _check_ship(config: ProjectConfig) -> None:
    from .ship import settings as ship_settings

    ship = config.ship
    if not isinstance(ship, dict):
        raise ValueError(msg("config.ship_object"))
    if ship.get("command") is not None:
        _text(ship["command"], "ship.command")
    has_command = isinstance(ship.get("command"), str) and bool(ship["command"].strip())
    if has_command:
        ship_settings(config)
    else:
        timeout = ship.get("timeout", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(msg("ship.timeout"))
    on_approve = ship.get("on_approve", False)
    if not isinstance(on_approve, bool):
        raise ValueError(msg("ship.on_approve_bool"))
    if on_approve and not has_command:
        raise ValueError(msg("ship.on_approve_command"))
    if ship.get("on_failure", "stay") not in SHIP_ON_FAILURE:
        raise ValueError(msg("ship.on_failure", values=", ".join(SHIP_ON_FAILURE)))


def check_harness(harness) -> None:
    if not isinstance(harness, dict) or harness.get("type", "claude") not in HARNESS_TYPES:
        raise ValueError(msg("config.harness_type", types=", ".join(HARNESS_TYPES)))
    for name in ("permission_mode", "model", "sandbox"):
        _optional_text(harness.get(name), "harness." + name)
    tools = harness.get("allowed_tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools)):
        raise ValueError(msg("config.allowed_tools"))


def _check_harness(config: ProjectConfig) -> None:
    if config.harness is not None:
        check_harness(config.harness)


def check_language(language) -> None:
    _text(language, "language", required=True)


def check_global_max_agents(value) -> None:
    _positive_int(value, "global_max_agents")


def _check_tracker(config: ProjectConfig) -> None:
    from .tracker import validate_config as validate_tracker

    validate_tracker(config.tracker)


def _check_chains(config: ProjectConfig) -> None:
    if not isinstance(config.chains, bool):
        raise ValueError(msg("config.chains"))


def _check_auto_approve(config: ProjectConfig) -> None:
    if not isinstance(config.auto_approve, bool):
        raise ValueError(msg("config.auto_approve"))


def _bool(value, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(msg("config.bool", name=name))


def _check_project_agent_status(config: ProjectConfig) -> None:
    if not isinstance(config.project_agent_status, bool):
        raise ValueError(msg("config.project_agent_status"))


def _check_kind(config: ProjectConfig) -> None:
    if config.kind not in PROJECT_KINDS:
        raise ValueError(msg("config.kind", kinds=", ".join(PROJECT_KINDS)))


# полные проверки каждого поля, которое задаёт пользователь (repos проверяются отдельно — с учётом прежних)
FIELD_CHECKS = {
    "title": lambda c: _text(c.title, "title", required=True),
    "chains": _check_chains,
    "auto_approve": _check_auto_approve,
    "project_agent_status": _check_project_agent_status,
    "project_agent_tasks": lambda c: _bool(c.project_agent_tasks, "project_agent_tasks"),
    "archived": lambda c: _bool(c.archived, "archived"),
    "max_agents": lambda c: _positive_int(c.max_agents, "max_agents"),
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
        if name in BOARD_FIELDS:
            raise ValueError(msg("config.board_field", name=name))
        if name in NOT_SETTABLE:
            raise ValueError(msg("config.not_settable", name=name, reason=msg(NOT_SETTABLE[name])))
        if name not in known:
            raise ValueError(msg("config.unknown_field", name=name))


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
        raise ValueError(msg("config.string", name="title"))
    _check_chains(config)
    _check_auto_approve(config)
    _check_project_agent_status(config)
    _check_kind(config)
    _bool(config.archived, "archived")
    _bool(config.project_agent_tasks, "project_agent_tasks")
    _positive_int(config.max_agents, "max_agents")
    for name in ("agent_rules", "project_brief", "decomposition_rules", "branch_template"):
        _text(getattr(config, name), name)
    _repos_structure(config.repos)
    tracker = config.tracker
    if not isinstance(tracker, dict) or not isinstance(tracker.get("type", "none"), str):
        raise ValueError(msg("config.tracker_object"))
    review = config.review
    if review is not None and (not isinstance(review, dict) or (review.get("tool") or "crit") not in REVIEW_TOOLS):
        raise ValueError(msg("config.review_tool", tools=", ".join(REVIEW_TOOLS)))
    if not isinstance(config.ship, dict):
        raise ValueError(msg("config.ship_object"))
    if not isinstance(config.resources, list):
        raise ValueError(msg("config.resources_list"))
    harness = config.harness
    if harness is not None and (not isinstance(harness, dict) or harness.get("type", "claude") not in HARNESS_TYPES):
        raise ValueError(msg("config.harness_type", types=", ".join(HARNESS_TYPES)))
    if not isinstance(config.links, list):
        raise ValueError(msg("config.links_list"))


def validate_config(config: ProjectConfig) -> None:
    """Все проверки настроек проекта целиком; ошибка — ValueError с понятным текстом."""
    validate_fields(config, ["repos"] + list(FIELD_CHECKS))
    validate_structure(config)
