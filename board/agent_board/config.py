from __future__ import annotations

from dataclasses import dataclass, field
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
