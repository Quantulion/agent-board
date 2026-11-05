from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import ProjectConfig
from .model import Event, Message, Proposal, Task

PROJECT_THREAD = "_project"


def board_home() -> Path:
    env = os.environ.get("AGENT_BOARD_HOME")
    return Path(env) if env else Path.home() / ".agent-board"


def list_projects() -> List[str]:
    projects = board_home() / "projects"
    if not projects.is_dir():
        return []
    return sorted(p.name for p in projects.iterdir() if p.is_dir())


def atomic_write_json(path: Path, obj) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                # оборванная запись в конце файла
                continue
    return items


def _append_jsonl(path: Path, obj) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


class ProjectStore:
    def __init__(self, slug: str):
        self.slug = slug
        self.root = board_home() / "projects" / slug
        self._last_seq = None

    def create(self, config: ProjectConfig) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "tasks").mkdir()
        (self.root / "threads").mkdir()
        (self.root / "reports").mkdir()
        self.save_config(config)

    def load_config(self) -> ProjectConfig:
        return ProjectConfig.from_dict(self._read_json("project.json"))

    def save_config(self, c: ProjectConfig) -> None:
        atomic_write_json(self.root / "project.json", c.to_dict())

    def load_tasks(self) -> Dict[str, Task]:
        tasks_dir = self.root / "tasks"
        result = {}
        if tasks_dir.is_dir():
            for p in sorted(tasks_dir.glob("*.json")):
                t = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
                result[t.key] = t
        return result

    def save_task(self, t: Task) -> None:
        atomic_write_json(self.root / "tasks" / (t.key + ".json"), t.to_dict())

    def append_message(self, thread: str, m: Message) -> None:
        _append_jsonl(self.root / "threads" / (thread + ".jsonl"), m.to_dict())

    def read_thread(self, thread: str) -> List[Message]:
        return [Message.from_dict(d) for d in _read_jsonl(self.root / "threads" / (thread + ".jsonl"))]

    def load_proposals(self) -> List[Proposal]:
        p = self.root / "proposals.json"
        if not p.exists():
            return []
        return [Proposal.from_dict(d) for d in json.loads(p.read_text(encoding="utf-8"))]

    def save_proposals(self, ps: List[Proposal]) -> None:
        atomic_write_json(self.root / "proposals.json", [p.to_dict() for p in ps])

    def load_meta(self) -> dict:
        p = self.root / "meta.json"
        if not p.exists():
            return {"project_unread": 0}
        return json.loads(p.read_text(encoding="utf-8"))

    def save_meta(self, d: dict) -> None:
        atomic_write_json(self.root / "meta.json", d)

    def append_event(self, type: str, data: dict) -> Event:
        if self._last_seq is None:
            self._last_seq = max((e.seq for e in self.read_events()), default=0)
        self._last_seq += 1
        at = datetime.now(timezone.utc).isoformat()
        e = Event(seq=self._last_seq, type=type, at=at, data=data)
        _append_jsonl(self.root / "events.jsonl", e.to_dict())
        return e

    def read_events(self, since: int = 0) -> List[Event]:
        events = [Event.from_dict(d) for d in _read_jsonl(self.root / "events.jsonl")]
        return [e for e in events if e.seq > since]

    def _read_json(self, name: str):
        return json.loads((self.root / name).read_text(encoding="utf-8"))
