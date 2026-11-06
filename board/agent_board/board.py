from __future__ import annotations

import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from .graph import dependents, display_status, open_deps, ready_tasks, validate_deps
from .model import Event, Message, Proposal, Question, Status, Task
from .ship import check_request, order as ship_order, settings as ship_settings
from .store import PROJECT_THREAD, ProjectStore, _check_ident

ACTORS = {"user", "agent", "tracker", "system"}
AGENT_FORBIDDEN = {Status.APPROVED, Status.DONE, Status.SENT, Status.CANCELLED}
RETURNED_FROM = {Status.IN_REVIEW, Status.APPROVED, Status.SENT, Status.DONE}
UPDATABLE_FIELDS = {
    "summary", "todo", "changed_files", "review", "ship", "worktree", "branch", "base_branch",
    "session_id", "report", "tracker_key", "tracker_url", "external_blocker",
    "description", "done_when", "title", "estimate",
    "agent_state", "turn_error", "last_turn_at", "harness", "turn_process", "rule_violations", "agent_parked",
}
# поля, которые ведёт только раннер: по ним он выполняет git-операции и управляет процессами
RUNNER_FIELDS = {
    "worktree", "branch", "base_branch", "session_id", "agent_state", "turn_error", "last_turn_at", "harness",
    "turn_process", "agent_parked", "review", "ship",
}
# нарушения правил фиксируют пользователь и агент проекта, но не агент самой задачи
RULE_VIOLATION_AUTHORS = {"user", "project_agent"}
MAX_RULE_VIOLATIONS = 50
MAX_RULE_CHARS = 200
MAX_RULE_TEXT_CHARS = 2000
LAST_MESSAGE_CHARS = 200
REBASE_CLOSED = {Status.DONE, Status.SENT, Status.CANCELLED}
CREATABLE_FIELDS = ("key", "title", "description", "done_when", "repo", "kind", "estimate", "deps")
TASK_KINDS = ("code", "research")
# стадии проекта: разбивка, заведение задач в трекере (необязательно), работа агентов, завершение
STAGE_TRANSITIONS = {
    "decomposition": ("tracker", "work"),
    "tracker": ("work",),
    "work": ("done",),
}

# Словарь событий журнала: тип -> поля data. Клиенты (UI, оркестратор агентов) опираются на него.
EVENT_TYPES: Dict[str, str] = {
    "task_added": "key",
    "status_changed": "key, from, to, actor",
    "task_updated": "key, fields (список изменённых полей)",
    "deps_changed": "key, deps",
    "task_unblocked": "key — задача стала готова к работе",
    "agent_stop": "key — агент задачи должен остановиться",
    "agent_wake": "key (ключ задачи или _project), reason, доп. поля по reason",
    "question_asked": "key, question_id",
    "question_answered": "key, question_id",
    "message_posted": "thread, message_id, author",
    "thread_read": "thread",
    "proposal_created": "id",
    "proposal_applied": "id",
    "proposal_rejected": "id",
    "turn_started": "key — раннер начал ход агента",
    "turn_finished": "key — ход агента завершён",
    "turn_failed": "key, error — ход агента завершился ошибкой",
    "task_committed": "key — раннер закоммитил результат задачи",
    "lock_waiting": "key, resource — задача ждёт общий ресурс",
    "lock_acquired": "key, resource",
    "lock_released": "key, resource",
    "stage_changed": "from, to — пользователь перевёл проект на другую стадию",
    "project_agent_changed": "state, parked — раннер изменил состояние агента проекта",
    "review_requested": "key — пользователь попросил открыть раунд ревью",
    "ship_requested": "keys — пользователь попросил отправить задачи в ревью (в порядке отправки)",
    "ship_started": "key — раннер запустил команду отправки",
    "ship_finished": "key, ok, exit_code — команда отправки завершена (exit_code пуст при таймауте)",
}

# Причины agent_wake -> дополнительные поля события.
WAKE_REASONS: Dict[str, str] = {
    "ready": "задача добавлена или возвращена из отмены и сразу готова",
    "unblocked": "закрылись зависимости или снят внешний блокер",
    "returned": "from — пользователь вернул задачу в работу с этого статуса",
    "answer": "пользователь ответил на вопрос агента",
    "message": "message_id, by (автор: user или project_agent), task (у агента проекта: задача, к которой "
               "пользователь оставил комментарий на стадии разбивки) — новое сообщение для агента",
    "proposal_rejected": "агенту проекта отклонили предложение",
    "escalation": "from, by (agent) — агент задачи написал в тред проекта",
    "manual": "пользователь сам запустил задачу без сессии агента",
    "rebase": "from — зависимость снова одобрена или закрыта, зависимой с сессией нужно подтянуть изменения",
    "interrupted": "ход агента был прерван, нужно продолжить",
    "queued_restart": "следующий ход ждал очереди, когда сервер перезапустился, нужно продолжить",
    "review": "round, comments [{id, path, line, body}], binary, crit_dir — пользователь завершил раунд ревью "
              "с комментариями",
}


def new_task_from_dict(d: Any) -> Task:
    """Новая задача из пользовательского ввода: только поля, которые задаются при создании."""
    if not isinstance(d, dict):
        raise ValueError("Задача должна быть объектом")
    extra = set(d) - set(CREATABLE_FIELDS)
    if extra:
        raise ValueError("Недопустимые поля задачи: %s" % ", ".join(sorted(map(str, extra))))
    for name in ("key", "title"):
        if not isinstance(d.get(name), str):
            raise ValueError("Поле задачи %s должно быть строкой" % name)
    for name in ("description", "done_when", "repo"):
        if name in d and not isinstance(d[name], str):
            raise ValueError("Поле задачи %s должно быть строкой" % name)
    if "kind" in d and (not isinstance(d["kind"], str) or d["kind"] not in TASK_KINDS):
        raise ValueError("Вид задачи должен быть одним из: %s" % ", ".join(TASK_KINDS))
    estimate = d.get("estimate")
    if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, (int, float))):
        raise ValueError("Оценка задачи должна быть числом")
    deps = d.get("deps", [])
    if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
        raise ValueError("deps задачи должен быть списком строк")
    return Task(**dict(d, deps=list(deps)))


def _clean_rule_violations(value: Any) -> List[dict]:
    """Проверяет нарушения правил и оставляет в них только правило и текст разумной длины."""
    if not isinstance(value, list) or len(value) > MAX_RULE_VIOLATIONS:
        raise ValueError("rule_violations должен быть списком не длиннее %d" % MAX_RULE_VIOLATIONS)
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("rule"), str) or not isinstance(item.get("text"), str):
            raise ValueError("Элемент rule_violations должен быть объектом со строками rule и text")
        result.append({"rule": item["rule"][:MAX_RULE_CHARS], "text": item["text"][:MAX_RULE_TEXT_CHARS]})
    return result


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
        self.terminal_command_for: Optional[Callable[[Task], Optional[str]]] = None
        # ресурсы, которые задача держит (position 0) или ждёт; ставит сервер, у которого очередь ресурсов
        self.locks_for: Optional[Callable[[str], List[dict]]] = None

    def record(self, type: str, data: dict) -> Event:
        """Записывает в журнал событие, произошедшее вне Board (например, события раннера)."""
        if type not in EVENT_TYPES:
            raise ValueError("Неизвестный тип события: %r" % type)
        with self._lock:
            return self._emit(type, dict(data))

    def update_meta(self, fields: dict) -> None:
        """Служебные поля meta.json (состояние агента проекта и т.п.), без событий."""
        with self._lock:
            meta = self.store.load_meta()
            meta.update(fields)
            self.store.save_meta(meta)

    def update_config(self, fields: dict) -> None:
        with self._lock:
            config = self.store.load_config()
            for name, value in fields.items():
                if not hasattr(config, name):
                    raise ValueError("Неизвестное поле конфига: %s" % name)
                setattr(config, name, value)
            self.store.save_config(config)

    def set_stage(self, stage: str, actor: str) -> List[Event]:
        """Переводит проект на следующую стадию; стадии меняет только пользователь."""
        with self._lock:
            if actor != "user":
                raise PermissionError("Стадию проекта меняет только пользователь")
            config = self.store.load_config()
            prev = config.stage
            if stage not in STAGE_TRANSITIONS.get(prev, ()):
                raise ValueError("Недопустимый переход стадии: %s -> %r" % (prev, stage))
            config.stage = stage
            self.store.save_config(config)
            return [self._emit("stage_changed", {"from": prev, "to": stage})]

    def snapshot(self) -> dict:
        with self._lock:
            config = self.store.load_config()
            tasks = self.store.load_tasks()
            meta = self.store.load_meta()
            items = []
            for key in sorted(tasks):
                t = tasks[key]
                d = t.to_dict()
                d["display_status"] = display_status(t, tasks, config.chains).value
                d["open_deps"] = open_deps(t, tasks, config.chains)
                d["terminal_command"] = self._terminal_command(t)
                size, last = self.store.thread_summary(key)
                d["thread_size"] = size
                d["last_message"] = None if last is None else {
                    "author": last.author, "text": last.text[:LAST_MESSAGE_CHARS], "at": last.at}
                d["open_questions"] = sum(1 for q in t.questions if q.answer is None)
                items.append(d)
            snap = {
                "config": config.to_dict(),
                "tasks": items,
                "proposals": [p.to_dict() for p in self.store.load_proposals()],
                "project_unread": meta["project_unread"],
                "project_thread_size": self.store.thread_summary(PROJECT_THREAD)[0],
                "project_agent": {
                    "state": meta.get("project_agent_state") or "idle",
                    "turn_error": meta.get("project_turn_error") or "",
                    "session_id": config.project_session_id,
                    "parked": bool(meta.get("project_agent_parked")),
                },
                "last_seq": self.store.last_seq(),
            }
        # очередь ресурсов сама пишет в доску под своей блокировкой: читаем её вне блокировки доски
        for d in snap["tasks"]:
            d["locks"] = self._locks(d["key"])
        return snap

    def _locks(self, key: str) -> List[dict]:
        hook = self.locks_for
        if hook is None:
            return []
        try:
            return hook(key)
        except Exception as exc:
            print("locks_for(%s) failed: %r" % (key, exc), file=sys.stderr)
            return []

    def add_task(self, task: Task, actor: str) -> List[Event]:
        with self._lock:
            self._reject_project_agent(actor)
            tasks = self.store.load_tasks()
            self._check_new_key(task.key, tasks)
            validate_deps(task.key, task.deps, tasks)
            task.status = Status.NOT_STARTED
            task.updated_at = self._clock()
            self.store.save_task(task)
            events: List[Event] = [self._emit("task_added", {"key": task.key})]
            if task.key in ready_tasks(self.store.load_tasks(), self._chains()):
                events.append(self._emit("agent_wake", {"key": task.key, "reason": "ready"}))
            return events

    def transition(self, key: str, to: Status, actor: str, wake_extra: Optional[dict] = None) -> List[Event]:
        """wake_extra — поля для пробуждения returned (например, комментарий пользователя к возврату)."""
        with self._lock:
            to = self._check_transition(key, to, actor)
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
                events.append(self._emit("agent_wake", dict(wake_extra or {}, key=key, reason="returned",
                                                            **{"from": prev.value})))
            if actor in ("user", "tracker") and to in (Status.APPROVED, Status.DONE):
                events.extend(self._rebase_wakes(key, affected))
            if actor == "user":
                events.extend(self._manual_wake(task, prev))
            return events

    def _check_transition(self, key: str, to: Status, actor: str) -> Status:
        """Проверяет переход без изменений; возвращает целевой статус."""
        self._reject_project_agent(actor)
        if actor not in ACTORS:
            raise ValueError("Неизвестный актор: %r" % actor)
        to = Status(to)
        if to == Status.BLOCKED:
            raise ValueError("Статус blocked вычисляется по зависимостям; внешнюю блокировку задают полем external_blocker")
        if actor == "agent" and to in AGENT_FORBIDDEN:
            raise PermissionError("Агент не может ставить статус %s" % to.value)
        self._get(self.store.load_tasks(), key)
        return to

    def transition_if(self, key: str, expected: Status, to: Status, actor: str) -> List[Event]:
        """Переводит задачу, только если она сейчас в статусе expected; иначе ничего не делает."""
        with self._lock:
            if self._get(self.store.load_tasks(), key).status != Status(expected):
                return []
            return self.transition(key, to, actor)

    # --- ревью ---

    def request_review(self, key: str, actor: str) -> List[Event]:
        """Пользователь просит открыть раунд ревью; сам раунд запускает раннер."""
        with self._lock:
            self._require_user(actor, "Ревью начинает только пользователь")
            task = self._get(self.store.load_tasks(), key)
            if task.status != Status.IN_REVIEW:
                raise ValueError("Ревью начинается только для задачи на ревью (сейчас %s)" % task.status.value)
            review = self.store.load_config().review
            if not isinstance(review, dict) or (review.get("tool") or "crit") == "none":
                raise ValueError("В проекте нет инструмента ревью: смотрите изменения во вкладке «Изменения»")
            if isinstance(task.review, dict) and task.review.get("state") == "open":
                raise ValueError("Раунд ревью уже открыт")
            return [self._emit("review_requested", {"key": key})]

    def request_ship(self, keys: Any, actor: str) -> List[Event]:
        """Пользователь просит отправить одобренные задачи в ревью; запросы выполняет раннер по порядку зависимостей."""
        with self._lock:
            self._require_user(actor, "Отправляет в ревью только пользователь")
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                raise ValueError("keys должен быть непустым списком ключей задач")
            if len(set(keys)) != len(keys):
                raise ValueError("Ключи задач в запросе повторяются")
            tasks = self.store.load_tasks()
            for key in keys:
                self._get(tasks, key)
            ship_settings(self.store.load_config())
            check_request(keys, tasks)
            for key in keys:
                if isinstance(tasks[key].ship, dict) and tasks[key].ship.get("state") in ("queued", "running"):
                    raise ValueError("Задача %s уже отправляется" % key)
            ordered = ship_order(keys, tasks)
            for key in ordered:
                self.update_task(key, "system", {"ship": {"state": "queued", "at": self._clock()}})
            return [self._emit("ship_requested", {"keys": ordered})]

    def approve(self, key: str, actor: str, note: Optional[str] = None) -> List[Event]:
        """Одобрение задачи на ревью; note — системное сообщение в тред (например, откуда одобрение)."""
        with self._lock:
            self._require_user(actor, "Одобряет только пользователь")
            task = self._get(self.store.load_tasks(), key)
            if task.status != Status.IN_REVIEW:
                raise ValueError("Одобрить можно только задачу на ревью (сейчас %s)" % task.status.value)
            events = self.transition(key, Status.APPROVED, actor)
            if note:
                events.extend(self.post_message(key, "system", note))
            return events

    def return_to_work(self, key: str, actor: str, text: str) -> List[Event]:
        """Возврат в работу с комментарием: агент получает комментарий вместе с пробуждением returned."""
        with self._lock:
            self._require_user(actor, "Вернуть в работу с комментарием может только пользователь")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Нужен комментарий")
            task = self._get(self.store.load_tasks(), key)
            if task.status not in RETURNED_FROM:
                raise ValueError("Вернуть в работу можно задачу на ревью или после него (сейчас %s)" % task.status.value)
            self._check_transition(key, Status.IN_PROGRESS, actor)
            msg, event = self._append_message(key, actor, text)
            return [event] + self.transition(key, Status.IN_PROGRESS, actor, wake_extra={"message_id": msg.id})

    @staticmethod
    def _require_user(actor: str, message: str) -> None:
        if actor not in ACTORS and actor != "project_agent":
            raise ValueError("Неизвестный актор: %r" % actor)
        if actor != "user":
            raise PermissionError(message)

    def _terminal_command(self, task: Task) -> Optional[str]:
        hook = self.terminal_command_for
        if hook is None or not task.session_id:
            return None
        try:
            return hook(task)
        except Exception as exc:
            print("terminal_command_for(%s) failed: %r" % (task.key, exc), file=sys.stderr)
            return None

    def _rebase_wakes(self, key: str, affected: List[str]) -> List[Event]:
        tasks = self.store.load_tasks()
        return [
            self._emit("agent_wake", {"key": k, "reason": "rebase", "from": key})
            for k in affected
            if k in tasks and tasks[k].session_id and tasks[k].status not in REBASE_CLOSED
        ]

    def _manual_wake(self, task: Task, prev: Status) -> List[Event]:
        to = task.status
        if to == Status.IN_PROGRESS and prev in (Status.NOT_STARTED, Status.BLOCKED) and not task.session_id:
            return [self._emit("agent_wake", {"key": task.key, "reason": "manual"})]
        if prev == Status.CANCELLED and to == Status.NOT_STARTED and task.key in self._ready(self.store.load_tasks()):
            return [self._emit("agent_wake", {"key": task.key, "reason": "ready"})]
        return []

    def update_task(self, key: str, actor: str, fields: dict) -> List[Event]:
        with self._lock:
            if not isinstance(fields, dict):
                raise ValueError("fields должен быть объектом")
            unknown = set(fields) - UPDATABLE_FIELDS
            if unknown:
                raise ValueError("Недопустимые поля: %s" % ", ".join(sorted(map(str, unknown))))
            owned = set(fields) & RUNNER_FIELDS
            if owned and actor != "system":
                raise PermissionError("Эти поля меняет только раннер: %s" % ", ".join(sorted(owned)))
            if "rule_violations" in fields:
                if actor not in RULE_VIOLATION_AUTHORS:
                    raise PermissionError("Нарушения правил фиксируют пользователь и агент проекта")
                fields = dict(fields, rule_violations=_clean_rule_violations(fields["rule_violations"]))
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
            now = self._clock()
            question.answer = {"choice": choice, "comment": comment, "at": now}
            task.updated_at = now
            self.store.save_task(task)
            text = "\n".join(p for p in (choice, comment) if p)
            msg = Message(id=uuid.uuid4().hex, author="user", text=text, at=now, question_id=qid)
            self.store.append_message(key, msg)
            events = [
                self._emit("question_answered", {"key": key, "question_id": qid}),
                self._emit("message_posted", {"thread": key, "message_id": msg.id, "author": "user"}),
                self._emit("agent_wake", {"key": key, "reason": "answer"}),
            ]
            if task.status == Status.WAITING_ANSWER and all(q.answer is not None for q in task.questions):
                events.extend(self.transition(key, Status.IN_PROGRESS, "system"))
            return events

    def post_message(self, thread: str, author: str, text: str, from_task: Optional[str] = None) -> List[Event]:
        """from_task — задача, из которой агент пишет в тред проекта; такое сообщение будит агента проекта."""
        with self._lock:
            task = None
            if thread != PROJECT_THREAD:
                task = self._get(self.store.load_tasks(), thread)
                from_task = None
            elif from_task is not None:
                if not isinstance(from_task, str):
                    raise ValueError("from_task должен быть строкой")
                self._get(self.store.load_tasks(), from_task)
            msg, posted = self._append_message(thread, author, text)
            events = [posted]
            wakes_agent = author == "user" or (author == "project_agent" and task is not None)
            if author == "user" and task is not None and self.store.load_config().stage == "decomposition":
                # агентов задач до утверждения разбивки нет: комментарий к задаче читает агент проекта
                events.append(self._emit("agent_wake", {"key": PROJECT_THREAD, "reason": "message", "task": thread,
                                                        "message_id": msg.id, "by": author}))
            elif wakes_agent:
                events.append(self._emit("agent_wake", {"key": thread, "reason": "message", "message_id": msg.id,
                                                        "by": author}))
            elif author == "agent" and from_task:
                events.append(self._emit("agent_wake", {"key": PROJECT_THREAD, "reason": "escalation", "from": from_task,
                                                        "by": author}))
            return events

    def _append_message(self, thread: str, author: str, text: str):
        """Сообщение в тред без пробуждений: (сообщение, событие message_posted)."""
        msg = Message(id=uuid.uuid4().hex, author=author, text=text, at=self._clock())
        self.store.append_message(thread, msg)
        if author != "user":
            if thread != PROJECT_THREAD:
                task = self._get(self.store.load_tasks(), thread)
                task.unread += 1
                self.store.save_task(task)
            else:
                meta = self.store.load_meta()
                meta["project_unread"] += 1
                self.store.save_meta(meta)
        return msg, self._emit("message_posted", {"thread": thread, "message_id": msg.id, "author": author})

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
                events = self.add_task(new_task_from_dict(proposal.payload["task"]), "user")
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
        if kind not in ("add_task", "set_deps", "cancel_task"):
            raise ValueError("Неизвестный вид предложения: %r" % kind)
        if not isinstance(payload, dict):
            raise ValueError("payload предложения должен быть объектом")
        tasks = self.store.load_tasks()
        if kind == "add_task":
            raw = payload.get("task")
            if not isinstance(raw, dict):
                raise ValueError("В предложении add_task нужен объект task")
            task = new_task_from_dict(raw)
            self._check_new_key(task.key, tasks)
            validate_deps(task.key, task.deps, tasks)
            return task.key
        key = payload.get("key")
        if not isinstance(key, str):
            raise ValueError("В предложении нужен строковый key")
        if kind == "set_deps":
            deps = payload.get("deps")
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ValueError("В предложении set_deps deps должен быть списком строк")
            key = self._get(tasks, key).key
            validate_deps(key, list(deps), tasks)
            return key
        return self._get(tasks, key).key

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
    def _check_new_key(key: str, tasks) -> None:
        if not isinstance(key, str):
            raise ValueError("Ключ задачи должен быть строкой")
        if key.startswith("_"):
            raise ValueError("Ключ задачи не может начинаться с «_»: %s" % key)
        _check_ident(key, "задача")
        # файлы задач на регистронезависимой ФС перезаписали бы друг друга
        clash = next((k for k in tasks if k.lower() == key.lower()), None)
        if clash is not None:
            raise ValueError("Задача уже существует: %s" % clash)

    @staticmethod
    def _reject_project_agent(actor: str) -> None:
        if actor == "project_agent":
            raise PermissionError("Агент проекта меняет граф только через предложения")

    def _emit(self, type: str, data: dict) -> Event:
        event = self.store.append_event(type, data, self._clock())
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
