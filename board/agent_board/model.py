from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    NOT_STARTED = "not_started"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    WAITING_ANSWER = "waiting_answer"
    QUEUED = "queued"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SENT = "sent"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Status.NOT_STARTED: "Не начата",
    Status.BLOCKED: "Заблокирована",
    Status.IN_PROGRESS: "В работе",
    Status.WAITING_ANSWER: "Ждёт ответа",
    Status.QUEUED: "Ждёт очереди",
    Status.IN_REVIEW: "На ревью",
    Status.APPROVED: "Одобрена",
    Status.SENT: "Отправлена",
    Status.DONE: "Готово",
    Status.CANCELLED: "Отменена",
}

DEP_SATISFIED_CHAINS = {Status.APPROVED, Status.SENT, Status.DONE}
DEP_SATISFIED_STRICT = {Status.DONE}


def _plain(value: Any) -> Any:
    if isinstance(value, Status):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class _Serializable:
    def to_dict(self) -> Dict[str, Any]:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Question(_Serializable):
    id: str
    text: str
    options: List[str] = field(default_factory=list)
    context: str = ""
    recommended: Optional[str] = None
    answer: Optional[dict] = None


@dataclass
class Message(_Serializable):
    id: str
    author: str
    text: str
    at: str
    question_id: Optional[str] = None


@dataclass
class Task(_Serializable):
    key: str
    title: str
    description: str = ""
    done_when: str = ""
    repo: str = ""
    kind: str = "code"
    estimate: Optional[float] = None
    deps: List[str] = field(default_factory=list)
    status: Status = Status.NOT_STARTED
    external_blocker: Optional[str] = None
    summary: str = ""
    todo: List[dict] = field(default_factory=list)
    questions: List[Question] = field(default_factory=list)
    session_id: Optional[str] = None
    worktree: Optional[str] = None
    branch: Optional[str] = None
    base_branch: Optional[str] = None
    tracker_key: Optional[str] = None
    tracker_url: Optional[str] = None
    review: Optional[dict] = None
    changed_files: List[str] = field(default_factory=list)
    report: Optional[str] = None
    unread: int = 0
    updated_at: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        d = dict(d)
        if "status" in d:
            d["status"] = Status(d["status"])
        if "questions" in d:
            d["questions"] = [q if isinstance(q, Question) else Question.from_dict(q) for q in d["questions"]]
        return super().from_dict(d)


@dataclass
class Proposal(_Serializable):
    id: str
    kind: str
    payload: dict
    reason: str
    status: str = "open"
    at: str = ""


@dataclass
class Event(_Serializable):
    seq: int
    type: str
    at: str
    data: dict
