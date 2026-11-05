from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, List, Optional, Set

from .graph import dependents, display_status, open_deps, ready_tasks, validate_deps
from .model import Event, Message, Proposal, Question, Status, Task
from .store import PROJECT_THREAD, ProjectStore

ACTORS = {"user", "agent", "tracker", "system"}
AGENT_FORBIDDEN = {Status.DONE, Status.SENT, Status.CANCELLED}
RETURNED_FROM = {Status.IN_REVIEW, Status.APPROVED, Status.SENT, Status.DONE}
UPDATABLE_FIELDS = {
    "summary", "todo", "changed_files", "review", "worktree", "branch", "base_branch",
    "session_id", "report", "tracker_key", "tracker_url", "external_blocker",
    "description", "done_when", "title", "estimate",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Board:
    """Единственный писатель состояния проекта: каждое изменение превращается в события журнала."""

    def __init__(
        self,
        store: ProjectStore,
        clock: Optional[Callable[[], str]] = None,
        on_event: Optional[Callable[[Event], None]] = None,
    ):
        self.store = store
        self._clock = clock or _utc_now
        self._on_event = on_event
        self._lock = threading.RLock()

    def snapshot(self) -> dict:
        with self._lock:
            config = self.store.load_config()
            tasks = self.store.load_tasks()
            items = []
            for key in sorted(tasks):
                t = tasks[key]
                d = t.to_dict()
                d["display_status"] = display_status(t, tasks, config.chains).value
                d["open_deps"] = open_deps(t, tasks, config.chains)
                items.append(d)
            return {
                "config": config.to_dict(),
                "tasks": items,
                "proposals": [p.to_dict() for p in self.store.load_proposals()],
                "project_unread": self.store.load_meta()["project_unread"],
                "last_seq": max((e.seq for e in self.store.read_events()), default=0),
            }

    def add_task(self, task: Task, actor: str) -> List[Event]:
        with self._lock:
            self._reject_project_agent(actor)
            tasks = self.store.load_tasks()
            if task.key in tasks:
                raise ValueError("Задача уже существует: %s" % task.key)
            validate_deps(task.key, task.deps, tasks)
            task.updated_at = self._clock()
            self.store.save_task(task)
            events: List[Event] = [self._emit("task_added", {"key": task.key})]
            if task.key in ready_tasks(self.store.load_tasks(), self._chains()):
                events.append(self._emit("agent_wake", {"key": task.key, "reason": "ready"}))
            return events

    def transition(self, key: str, to: Status, actor: str) -> List[Event]:
        with self._lock:
            self._reject_project_agent(actor)
            if actor not in ACTORS:
                raise ValueError("Неизвестный актор: %r" % actor)
            to = Status(to)
            if actor == "agent" and to in AGENT_FORBIDDEN:
                raise PermissionError("Агент не может ставить статус %s" % to.value)
            tasks = self.store.load_tasks()
            task = self._get(tasks, key)
            before = self._ready(tasks)
            affected = dependents(key, tasks)
            prev = task.status
            task.status = to
            task.updated_at = self._clock()
            self.store.save_task(task)
            events = [self._emit("status_changed", {"key": key, "from": prev.value, "to": to.value, "actor": actor})]

            if to == Status.CANCELLED:
                events.append(self._emit("agent_stop", {"key": key}))
                for dep_key in affected:
                    t = tasks[dep_key]
                    t.deps = [d for d in t.deps if d != key]
                    t.updated_at = self._clock()
                    self.store.save_task(t)
                    events.append(self._emit("deps_changed", {"key": dep_key, "deps": list(t.deps)}))

            events.extend(self._unblocked(before, affected))
            if actor in ("user", "tracker") and to == Status.IN_PROGRESS and prev in RETURNED_FROM:
                events.append(self._emit("agent_wake", {"key": key, "reason": "returned", "from": prev.value}))
            return events

    def update_task(self, key: str, actor: str, **fields) -> List[Event]:
        with self._lock:
            unknown = set(fields) - UPDATABLE_FIELDS
            if unknown:
                raise ValueError("Недопустимые поля: %s" % ", ".join(sorted(unknown)))
            tasks = self.store.load_tasks()
            task = self._get(tasks, key)
            before = self._ready(tasks)
            for name, value in fields.items():
                setattr(task, name, value)
            task.updated_at = self._clock()
            self.store.save_task(task)
            events = [self._emit("task_updated", {"key": key, "fields": sorted(fields)})]
            if "external_blocker" in fields:
                events.extend(self._unblocked(before, [key]))
            return events

    def set_deps(self, key: str, deps: List[str], actor: str) -> List[Event]:
        with self._lock:
            self._reject_project_agent(actor)
            tasks = self.store.load_tasks()
            task = self._get(tasks, key)
            validate_deps(key, deps, tasks)
            before = self._ready(tasks)
            task.deps = list(deps)
            task.updated_at = self._clock()
            self.store.save_task(task)
            events = [self._emit("deps_changed", {"key": key, "deps": list(deps)})]
            events.extend(self._unblocked(before, [key]))
            return events

    def ask(self, key: str, question: Question) -> List[Event]:
        with self._lock:
            task = self._get(self.store.load_tasks(), key)
            if not question.id:
                question.id = "q%d" % (len(task.questions) + 1)
            task.questions.append(question)
            task.updated_at = self._clock()
            self.store.save_task(task)
            events = [self._emit("question_asked", {"key": key, "question_id": question.id})]
            if task.status != Status.WAITING_ANSWER:
                events.extend(self.transition(key, Status.WAITING_ANSWER, "agent"))
            return events

    def answer(self, key: str, qid: str, choice: Optional[str], comment: str) -> List[Event]:
        with self._lock:
            task = self._get(self.store.load_tasks(), key)
            question = next((q for q in task.questions if q.id == qid), None)
            if question is None:
                raise KeyError("Неизвестный вопрос: %s" % qid)
            if question.answer is not None:
                raise ValueError("На вопрос уже ответили: %s" % qid)
            question.answer = {"choice": choice, "comment": comment}
            now = self._clock()
            task.updated_at = now
            self.store.save_task(task)
            text = "\n".join(p for p in (choice, comment) if p)
            self.store.append_message(key, Message(id=uuid.uuid4().hex, author="user", text=text, at=now, question_id=qid))
            events = [
                self._emit("question_answered", {"key": key, "question_id": qid}),
                self._emit("agent_wake", {"key": key, "reason": "answer"}),
            ]
            if task.status == Status.WAITING_ANSWER and all(q.answer is not None for q in task.questions):
                events.extend(self.transition(key, Status.IN_PROGRESS, "system"))
            return events

    def post_message(self, thread: str, author: str, text: str) -> List[Event]:
        with self._lock:
            task = None
            if thread != PROJECT_THREAD:
                task = self._get(self.store.load_tasks(), thread)
            now = self._clock()
            msg = Message(id=uuid.uuid4().hex, author=author, text=text, at=now)
            self.store.append_message(thread, msg)
            if author != "user":
                if task is not None:
                    task.unread += 1
                    self.store.save_task(task)
                else:
                    meta = self.store.load_meta()
                    meta["project_unread"] += 1
                    self.store.save_meta(meta)
            events = [self._emit("message_posted", {"thread": thread, "message_id": msg.id, "author": author})]
            wakes_agent = author == "user" or (author == "project_agent" and task is not None)
            if wakes_agent:
                events.append(self._emit("agent_wake", {"key": thread, "reason": "message"}))
            return events

    def mark_read(self, thread: str) -> List[Event]:
        with self._lock:
            if thread == PROJECT_THREAD:
                meta = self.store.load_meta()
                meta["project_unread"] = 0
                self.store.save_meta(meta)
            else:
                task = self._get(self.store.load_tasks(), thread)
                task.unread = 0
                self.store.save_task(task)
            return [self._emit("thread_read", {"thread": thread})]

    def propose(self, kind: str, payload: dict, reason: str) -> List[Event]:
        with self._lock:
            self._check_proposal(kind, payload)
            proposals = self.store.load_proposals()
            pid = "p%d" % (len(proposals) + 1)
            proposals.append(Proposal(id=pid, kind=kind, payload=payload, reason=reason, at=self._clock()))
            self.store.save_proposals(proposals)
            return [self._emit("proposal_created", {"id": pid})]

    def apply_proposal(self, pid: str) -> List[Event]:
        with self._lock:
            proposals = self.store.load_proposals()
            proposal = self._open_proposal(proposals, pid)
            # граф мог измениться с момента создания предложения
            affected = self._check_proposal(proposal.kind, proposal.payload)
            if proposal.kind == "add_task":
                events = self.add_task(Task.from_dict(proposal.payload["task"]), "user")
            elif proposal.kind == "set_deps":
                events = self.set_deps(proposal.payload["key"], list(proposal.payload["deps"]), "user")
            else:
                events = self.transition(proposal.payload["key"], Status.CANCELLED, "user")
            proposal.status = "applied"
            self.store.save_proposals(proposals)
            events.append(self._emit("proposal_applied", {"id": pid}))
            text = "Применено предложение %s: %s" % (pid, self._describe_proposal(proposal))
            for thread in (PROJECT_THREAD, affected):
                events.extend(self.post_message(thread, "system", text))
            return events

    def reject_proposal(self, pid: str) -> List[Event]:
        with self._lock:
            proposals = self.store.load_proposals()
            proposal = self._open_proposal(proposals, pid)
            proposal.status = "rejected"
            self.store.save_proposals(proposals)
            return [
                self._emit("proposal_rejected", {"id": pid}),
                self._emit("agent_wake", {"key": PROJECT_THREAD, "reason": "proposal_rejected"}),
            ]

    def _check_proposal(self, kind: str, payload: dict) -> str:
        """Проверяет предложение на текущем состоянии графа, возвращает ключ затронутой задачи."""
        tasks = self.store.load_tasks()
        if kind == "add_task":
            task = Task.from_dict(payload["task"])
            if task.key in tasks:
                raise ValueError("Задача уже существует: %s" % task.key)
            validate_deps(task.key, task.deps, tasks)
            return task.key
        if kind == "set_deps":
            key = self._get(tasks, payload["key"]).key
            validate_deps(key, list(payload["deps"]), tasks)
            return key
        if kind == "cancel_task":
            return self._get(tasks, payload["key"]).key
        raise ValueError("Неизвестный вид предложения: %r" % kind)

    @staticmethod
    def _open_proposal(proposals: List[Proposal], pid: str) -> Proposal:
        proposal = next((p for p in proposals if p.id == pid), None)
        if proposal is None:
            raise KeyError("Неизвестное предложение: %s" % pid)
        if proposal.status != "open":
            raise ValueError("Предложение уже обработано: %s (%s)" % (pid, proposal.status))
        return proposal

    @staticmethod
    def _describe_proposal(proposal: Proposal) -> str:
        p = proposal.payload
        if proposal.kind == "add_task":
            return "добавлена задача %s" % p["task"]["key"]
        if proposal.kind == "set_deps":
            return "зависимости задачи %s: %s" % (p["key"], ", ".join(p["deps"]) or "нет")
        return "отменена задача %s" % p["key"]

    @staticmethod
    def _reject_project_agent(actor: str) -> None:
        if actor == "project_agent":
            raise PermissionError("Агент проекта меняет граф только через предложения")

    def _emit(self, type: str, data: dict) -> Event:
        event = self.store.append_event(type, data)
        if self._on_event is not None:
            self._on_event(event)
        return event

    def _chains(self) -> bool:
        return self.store.load_config().chains

    def _ready(self, tasks) -> Set[str]:
        return set(ready_tasks(tasks, self._chains()))

    def _unblocked(self, before: Set[str], candidates: List[str]) -> List[Event]:
        # будим только тех, кто стал готов именно сейчас
        after = self._ready(self.store.load_tasks())
        events: List[Event] = []
        for key in sorted(set(candidates)):
            if key in after and key not in before:
                events.append(self._emit("task_unblocked", {"key": key}))
                events.append(self._emit("agent_wake", {"key": key, "reason": "unblocked"}))
        return events

    @staticmethod
    def _get(tasks, key: str) -> Task:
        if key not in tasks:
            raise KeyError("Неизвестная задача: %s" % key)
        return tasks[key]
