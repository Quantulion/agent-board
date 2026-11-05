from __future__ import annotations

from dataclasses import dataclass, field
import re
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
    review: dict = field(default_factory=lambda: {"tool": "crit"})
    ship_command: Optional[str] = None
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
