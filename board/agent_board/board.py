from __future__ import annotations

import copy
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from .config import (
    LEGACY_SHIP_COMMAND, ProjectConfig, check_field_names, clean_links, validate_config as validate_project_config,
    validate_fields, validate_structure,
)
from .graph import dependents, display_status, open_deps, ready_tasks, validate_deps
from .messages import Msg, localize, msg, normalize
from .model import Event, Message, Proposal, Question, Status, Task
from .ship import check_auto, check_request, is_shipping, order as ship_order, settings as ship_settings
from .tracker import check_issue_key, is_http_url, validate_config as validate_tracker
from .store import PROJECT_THREAD, ProjectStore, _check_ident

ACTORS = {"user", "agent", "tracker", "system"}
AGENT_FORBIDDEN = {Status.APPROVED, Status.DONE, Status.SENT, Status.CANCELLED}
RETURNED_FROM = {Status.IN_REVIEW, Status.APPROVED, Status.SENT, Status.DONE}
UPDATABLE_FIELDS = {
    "summary", "todo", "changed_files", "review", "ship", "worktree", "branch", "base_branch",
    "session_id", "report", "tracker_key", "tracker_url", "external_blocker",
    "description", "done_when", "title", "estimate",
    "agent_state", "turn_error", "last_turn_at", "harness", "turn_process", "rule_violations", "agent_parked",
    "wait_reason",
}
# поля, которые ведёт только раннер: по ним он выполняет git-операции и управляет процессами
RUNNER_FIELDS = {
    "worktree", "branch", "base_branch", "session_id", "agent_state", "turn_error", "last_turn_at", "harness",
    "turn_process", "agent_parked", "review", "ship", "wait_reason",
}
# привязку к задаче в трекере ставит доска при заведении задач или пользователь для уже существующей задачи
TRACKER_LINK_FIELDS = {"tracker_key", "tracker_url"}
MAX_TRACKER_KEY_CHARS = 100
# формулировку задачи меняет только пользователь: агент не переписывает себе критерий готовности
TASK_TEXT_FIELDS = {"title", "description", "done_when", "estimate"}
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
    "status_changed": "key, from, to, actor; auto — одобрено доской по настройке auto_approve",
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
    "proposal_obsolete": "id, reason (exists — задача с этим ключом уже есть, cancelled — задача отменена, "
                         "missing — задачи нет) — предложение устарело",
    "turn_started": "key — раннер начал ход агента",
    "turn_finished": "key — ход агента завершён",
    "turn_failed": "key, error — ход агента завершился ошибкой",
    "task_committed": "key — раннер закоммитил результат задачи",
    "lock_waiting": "key, resource — задача ждёт общий ресурс",
    "lock_acquired": "key, resource",
    "lock_released": "key, resource",
    "stage_changed": "from, to — пользователь перевёл проект на другую стадию",
    "config_changed": "fields [имена изменённых настроек] — пользователь изменил настройки проекта",
    "config_migrated": "field, to, moved — устаревшее поле настроек перенесено на новое место при чтении",
    "project_agent_changed": "state, parked, wait_reason — раннер изменил состояние агента проекта",
    "review_requested": "key — пользователь попросил открыть раунд ревью",
    "ship_requested": "keys — пользователь попросил отправить задачи в ревью (в порядке отправки)",
    "ship_started": "key — раннер запустил команду отправки",
    "ship_finished": "key, ok, exit_code — команда отправки завершена (exit_code пуст при таймауте)",
    "tracker_created": "created [ключи], linked [[блокер, блокируемая]], errors [ключи] — пользователь завёл "
                       "задачи в трекере",
    "tracker_synced": "ok, changed [ключи задач, переведённых по статусам трекера], error — итог проверки статусов "
                      "в трекере (пишется только при изменениях)",
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
    "rebase": "from — зависимость снова одобрена или закрыта, зависимой с сессией нужно подтянуть изменения; "
              "by (user или project_agent) — перенос на текущую базу запрошен вручную",
    "interrupted": "ход агента был прерван, нужно продолжить",
    "retry": "error — пользователь повторил упавший ход агента, нужно продолжить с места остановки",
    "queued_restart": "следующий ход ждал очереди, когда сервер перезапустился, нужно продолжить",
    "review": "round, comments [{id, path, line, body}], binary, crit_dir — пользователь завершил раунд ревью "
              "с комментариями",
}


def new_task_from_dict(d: Any) -> Task:
    """Новая задача из пользовательского ввода: только поля, которые задаются при создании."""
    if not isinstance(d, dict):
        raise ValueError(msg("board.task_not_object"))
    extra = set(d) - set(CREATABLE_FIELDS)
    if extra:
        raise ValueError(msg("board.task_extra_fields", names=", ".join(sorted(map(str, extra)))))
    for name in ("key", "title"):
        if not isinstance(d.get(name), str):
            raise ValueError(msg("board.task_field_str", name=name))
    for name in ("description", "done_when", "repo"):
        if name in d and not isinstance(d[name], str):
            raise ValueError(msg("board.task_field_str", name=name))
    if "kind" in d and (not isinstance(d["kind"], str) or d["kind"] not in TASK_KINDS):
        raise ValueError(msg("board.task_kind", kinds=", ".join(TASK_KINDS)))
    estimate = d.get("estimate")
    if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, (int, float))):
        raise ValueError(msg("board.task_estimate"))
    deps = d.get("deps", [])
    if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
        raise ValueError(msg("board.task_deps"))
    return Task(**dict(d, deps=list(deps)))


def _check_tracker_link(fields: dict) -> None:
    key = fields.get("tracker_key")
    if key is not None and (not isinstance(key, str) or not key or len(key) > MAX_TRACKER_KEY_CHARS
                            or any(c.isspace() for c in key)):
        raise ValueError(msg("board.tracker_key", max=MAX_TRACKER_KEY_CHARS))
    url = fields.get("tracker_url")
    if url is not None and not is_http_url(url):
        raise ValueError(msg("board.tracker_url"))


def _check_task_text(fields: dict) -> None:
    for name in ("title", "description", "done_when"):
        if name in fields and not isinstance(fields[name], str):
            raise ValueError(msg("board.task_field_str", name=name))
    estimate = fields.get("estimate")
    if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, (int, float))):
        raise ValueError(msg("board.task_estimate"))


def _clean_rule_violations(value: Any) -> List[dict]:
    """Проверяет нарушения правил и оставляет в них только правило и текст разумной длины."""
    if not isinstance(value, list) or len(value) > MAX_RULE_VIOLATIONS:
        raise ValueError(msg("board.rule_violations", max=MAX_RULE_VIOLATIONS))
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("rule"), str) or not isinstance(item.get("text"), str):
            raise ValueError(msg("board.rule_violation_item"))
        result.append({"rule": item["rule"][:MAX_RULE_CHARS], "text": item["text"][:MAX_RULE_TEXT_CHARS]})
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# сколько раз set_config пробует сохранить настройки, если их одновременно меняют
CONFIG_ATTEMPTS = 3


class ConfigConflict(Exception):
    """Настройки меняются из другого места быстрее, чем проходит проверка."""


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
        self._migrate_config()

    def _localized(self, value):
        """Сообщения каталога в сохраняемых данных переводятся на язык проекта в момент записи."""
        return localize(value, normalize(self.store.load_config().language))

    def record(self, type: str, data: dict) -> Event:
        """Записывает в журнал событие, произошедшее вне Board (например, события раннера)."""
        if type not in EVENT_TYPES:
            raise ValueError(msg("board.unknown_event", type=type))
        with self._lock:
            return self._emit(type, dict(data))

    def update_meta(self, fields: dict) -> None:
        """Служебные поля meta.json (состояние агента проекта и т.п.), без событий."""
        with self._lock:
            meta = self.store.load_meta()
            meta.update(self._localized(fields))
            self.store.save_meta(meta)

    def update_config(self, fields: dict) -> ProjectConfig:
        """Служебная правка настроек (раннер, тесты) под блокировкой доски; проверяются только переданные команда
        отправки и трекер."""
        check_field_names([name for name in fields if name != "project_session_id"])
        with self._lock:
            config = self.store.load_config()
            for name, value in fields.items():
                setattr(config, name, value)
            ship = config.ship if isinstance(config.ship, dict) else {}
            if "ship" in fields and ship.get("command"):
                ship_settings(config)
            if "tracker" in fields:
                validate_tracker(config.tracker)
            self.store.save_config(config)
            return config

    def set_config(self, actor: str, fields: Any, dry_run: bool = False) -> dict:
        """Настройки от пользователя; dry_run только проверяет. Возвращает {config, saved, events}.

        Медленные проверки (диск, git, трекер) идут до блокировки доски и только для изменившихся полей; под блокировкой
        настройки перечитываются, и итог проверяется по форме. Если заданное поле успели изменить, пока шли проверки,
        попытка повторяется. Пустой fields — полная проверка сохранённых настроек."""
        self._require_user(actor, msg("board.config_user_only"))
        if not isinstance(fields, dict):
            raise ValueError(msg("board.fields_not_object"))
        check_field_names(fields)
        for _ in range(CONFIG_ATTEMPTS):
            base = self.store.load_config()
            draft = self._merged_config(base, fields)
            if fields:
                changed = [name for name in fields if getattr(draft, name) != getattr(base, name)]
                validate_fields(draft, changed, base.repos)
            else:
                validate_project_config(draft)
            with self._lock:
                current = self.store.load_config()
                if any(getattr(current, name) != getattr(base, name) for name in fields):
                    continue
                config = self._merged_config(current, fields)
                validate_structure(config)
                config.links = clean_links(config.links)
                if not dry_run:
                    self.store.save_config(config)
                result = config.to_dict()
                before = current.to_dict()
                changed = sorted(k for k in fields if result.get(k) != before.get(k))
                events = [] if dry_run or not changed else [self._emit("config_changed", {"fields": changed})]
                return {"config": result, "saved": not dry_run, "events": [e.to_dict() for e in events]}
        raise ConfigConflict(msg("board.config_conflict", names=", ".join(sorted(fields))))

    @staticmethod
    def _merged_config(config: ProjectConfig, fields: dict) -> ProjectConfig:
        merged = copy.deepcopy(config)
        for name, value in fields.items():
            setattr(merged, name, copy.deepcopy(value))
        return merged

    def _migrate_config(self) -> None:
        """Переносит устаревшие поля сохранённых настроек на новое место; перенос записывается в журнал один раз."""
        path = self.store.root / "project.json"
        if not path.exists():
            return
        with self._lock:
            raw = self.store._read_json("project.json")
            if not isinstance(raw, dict) or LEGACY_SHIP_COMMAND not in raw:
                return
            config = ProjectConfig.from_dict(raw)
            moved = isinstance(config.ship, dict) and config.ship.get("command") == raw[LEGACY_SHIP_COMMAND] \
                and not (isinstance(raw.get("ship"), dict) and raw["ship"].get("command"))
            try:
                self.store.save_config(config)
                # без on_event: доску создают под блокировкой хаба, подписчикам это событие не нужно
                self.store.append_event("config_migrated", {"field": LEGACY_SHIP_COMMAND, "to": "ship.command",
                                                            "moved": bool(moved)}, self._clock())
            except OSError as e:
                # проект остаётся рабочим: настройки читаются с переносом в памяти, запись повторится при следующем чтении
                print("не удалось сохранить перенос настроек проекта %s: %s" % (self.store.slug, e), file=sys.stderr)

    def set_stage(self, stage: str, actor: str) -> List[Event]:
        """Переводит проект на следующую стадию; стадии меняет только пользователь."""
        with self._lock:
            if actor != "user":
                raise PermissionError(msg("board.stage_user_only"))
            config = self.store.load_config()
            prev = config.stage
            if stage not in STAGE_TRANSITIONS.get(prev, ()):
                raise ValueError(msg("board.bad_stage", prev=prev, stage=stage))
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
                    "wait_reason": meta.get("project_wait_reason") or "",
                    "session_id": config.project_session_id,
                    "parked": bool(meta.get("project_agent_parked")),
                },
                "tracker_sync": meta.get("tracker_sync"),
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
        """Новую задачу добавляет только пользователь; агенты предлагают её через агента проекта (propose)."""
        with self._lock:
            self._reject_project_agent(actor)
            self._require_user(actor, msg("board.add_task_user_only"))
            tasks = self.store.load_tasks()
            self._check_new_key(task.key, tasks)
            self._check_repo(task.repo)
            validate_deps(task.key, task.deps, tasks)
            task.status = Status.NOT_STARTED
            task.updated_at = self._clock()
            self.store.save_task(task)
            events: List[Event] = [self._emit("task_added", {"key": task.key})]
            if task.key in ready_tasks(self.store.load_tasks(), self._chains()):
                events.append(self._emit("agent_wake", {"key": task.key, "reason": "ready"}))
            events.extend(self._retire_proposals())
            return events

    def transition(self, key: str, to: Status, actor: str, wake_extra: Optional[dict] = None,
                   fields: Optional[dict] = None, auto: bool = False) -> List[Event]:
        """wake_extra — поля для пробуждения returned (например, комментарий пользователя к возврату).

        fields — служебные поля раннера, которые меняются вместе со статусом, чтобы их не видели порознь.
        auto — одобрение доской по настройке auto_approve: действует как одобрение пользователя."""
        with self._lock:
            to = self._check_transition(key, to, actor)
            fields = dict(fields or {})
            if fields and (actor != "system" or not set(fields) <= RUNNER_FIELDS):
                raise PermissionError(msg("board.runner_fields", names=", ".join(sorted(map(str, fields)))))
            tasks = self.store.load_tasks()
            task = self._get(tasks, key)
            before = self._ready(tasks)
            affected = dependents(key, tasks)
            prev = task.status
            task.status = to
            for name, value in fields.items():
                setattr(task, name, value)
            task.updated_at = self._clock()
            self.store.save_task(task)
            change = {"key": key, "from": prev.value, "to": to.value, "actor": actor}
            if auto:
                change["auto"] = True
            events = [self._emit("status_changed", change)]
            if fields:
                events.append(self._emit("task_updated", {"key": key, "fields": sorted(fields)}))

            if to == Status.CANCELLED:
                events.append(self._emit("agent_stop", {"key": key}))
                for dep_key in affected:
                    t = tasks[dep_key]
                    t.deps = [d for d in t.deps if d != key]
                    t.updated_at = self._clock()
                    self.store.save_task(t)
                    events.append(self._emit("deps_changed", {"key": dep_key, "deps": list(t.deps)}))
                events.extend(self._retire_proposals())

            events.extend(self._unblocked(before, affected))
            if actor in ("user", "tracker") and to == Status.IN_PROGRESS and prev in RETURNED_FROM:
                events.append(self._emit("agent_wake", dict(wake_extra or {}, key=key, reason="returned",
                                                            **{"from": prev.value})))
            if (actor in ("user", "tracker") or auto) and to in (Status.APPROVED, Status.DONE):
                events.extend(self._rebase_wakes(key, affected))
            if actor == "user":
                events.extend(self._manual_wake(task, prev))
            if actor == "agent" and to == Status.IN_REVIEW and task.kind != "research" \
                    and self.store.load_config().auto_approve is True:
                events.extend(self.transition(key, Status.APPROVED, "system", auto=True))
                events.extend(self.post_message(key, "system", msg("board.auto_approved")))
            return events

    def _check_transition(self, key: str, to: Status, actor: str) -> Status:
        """Проверяет переход без изменений; возвращает целевой статус."""
        self._reject_project_agent(actor)
        if actor not in ACTORS:
            raise ValueError(msg("board.unknown_actor", actor=actor))
        to = Status(to)
        if to == Status.BLOCKED:
            raise ValueError(msg("board.blocked_computed"))
        if actor == "agent" and to in AGENT_FORBIDDEN:
            raise PermissionError(msg("board.agent_status", status=to.value))
        self._get(self.store.load_tasks(), key)
        return to

    def transition_if(self, key: str, expected: Status, to: Status, actor: str,
                      fields: Optional[dict] = None) -> List[Event]:
        """Переводит задачу, только если она сейчас в статусе expected; иначе ничего не делает."""
        with self._lock:
            if self._get(self.store.load_tasks(), key).status != Status(expected):
                return []
            return self.transition(key, to, actor, fields=fields)

    # --- ревью ---

    def request_review(self, key: str, actor: str) -> List[Event]:
        """Пользователь просит открыть раунд ревью; сам раунд запускает раннер."""
        with self._lock:
            self._require_user(actor, msg("board.review_user_only"))
            task = self._get(self.store.load_tasks(), key)
            if task.status != Status.IN_REVIEW:
                raise ValueError(msg("board.review_not_in_review", status=task.status.value))
            review = self.store.load_config().review
            if not isinstance(review, dict) or (review.get("tool") or "crit") == "none":
                raise ValueError(msg("board.no_review_tool"))
            if isinstance(task.review, dict) and task.review.get("state") == "open":
                raise ValueError(msg("board.review_open"))
            return [self._emit("review_requested", {"key": key})]

    def request_ship(self, keys: Any, actor: str) -> List[Event]:
        """Пользователь просит отправить одобренные задачи в ревью; запросы выполняет раннер по порядку зависимостей."""
        with self._lock:
            self._require_user(actor, msg("board.ship_user_only"))
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
                raise ValueError(msg("board.ship_keys"))
            if len(set(keys)) != len(keys):
                raise ValueError(msg("board.ship_duplicate"))
            tasks = self.store.load_tasks()
            for key in keys:
                self._get(tasks, key)
            ship_settings(self.store.load_config())
            check_request(keys, tasks)
            for key in keys:
                if is_shipping(tasks[key]):
                    raise ValueError(msg("board.ship_in_progress", key=key))
            ordered = ship_order(keys, tasks)
            for key in ordered:
                self.update_task(key, "system", {"ship": {"state": "queued", "at": self._clock()}})
            return [self._emit("ship_requested", {"keys": ordered})]

    def request_auto_ship(self, key: str) -> List[Event]:
        """Отправка одобренной задачи по настройке ship.on_approve: та же очередь, что у запроса пользователя."""
        with self._lock:
            tasks = self.store.load_tasks()
            self._get(tasks, key)
            ship_settings(self.store.load_config())
            check_auto(key, tasks)
            self.update_task(key, "system", {"ship": {"state": "queued", "at": self._clock()}})
            return [self._emit("ship_requested", {"keys": [key], "auto": True})]

    def approve(self, key: str, actor: str, note: Optional[str] = None) -> List[Event]:
        """Одобрение задачи на ревью; note — системное сообщение в тред (например, откуда одобрение)."""
        with self._lock:
            self._require_user(actor, msg("board.approve_user_only"))
            task = self._get(self.store.load_tasks(), key)
            if task.status != Status.IN_REVIEW:
                raise ValueError(msg("board.approve_not_in_review", status=task.status.value))
            events = self.transition(key, Status.APPROVED, actor)
            if note:
                events.extend(self.post_message(key, "system", note))
            return events

    def retry(self, key: str, actor: str) -> List[Event]:
        """Пользователь повторяет упавший ход агента задачи или агента проекта; ход запускает раннер."""
        with self._lock:
            self._require_user(actor, msg("board.retry_user_only"))
            if key == PROJECT_THREAD:
                meta = self.store.load_meta()
                state, error = meta.get("project_agent_state") or "idle", meta.get("project_turn_error") or ""
            else:
                task = self._get(self.store.load_tasks(), key)
                if task.status in (Status.CANCELLED, Status.APPROVED, Status.SENT):
                    raise ValueError(msg("board.retry_status", status=task.status.value))
                if self.store.load_config().stage != "work":
                    raise ValueError(msg("board.retry_stage"))
                state, error = task.agent_state, task.turn_error
            if state != "error":
                raise ValueError(msg("board.retry_not_failed", key=key, state=state))
            return [self._emit("agent_wake", {"key": key, "reason": "retry", "error": error})]

    def request_rebase(self, key: str, actor: str) -> List[Event]:
        """Перенос ветки задачи на текущую базу с незакоммиченной работой; переносит раннер перед ходом агента,
        поэтому идущий ход перенос дожидается."""
        with self._lock:
            if actor not in ACTORS and actor != "project_agent":
                raise ValueError(msg("board.unknown_actor", actor=actor))
            if actor not in ("user", "project_agent"):
                raise PermissionError(msg("board.rebase_actor"))
            task = self._get(self.store.load_tasks(), key)
            if task.status in (Status.APPROVED, Status.SENT, Status.DONE, Status.CANCELLED):
                raise ValueError(msg("board.rebase_status", status=task.status.value))
            if not task.session_id or not task.worktree:
                raise ValueError(msg("board.rebase_no_worktree"))
            if self.store.load_config().stage != "work":
                raise ValueError(msg("board.retry_stage"))
            return [self._emit("agent_wake", {"key": key, "reason": "rebase", "by": actor})]

    def return_to_work(self, key: str, actor: str, text: str) -> List[Event]:
        """Возврат в работу с комментарием: агент получает комментарий вместе с пробуждением returned."""
        with self._lock:
            self._require_user(actor, msg("board.return_user_only"))
            if not isinstance(text, str) or not text.strip():
                raise ValueError(msg("board.comment_required"))
            task = self._get(self.store.load_tasks(), key)
            if task.status not in RETURNED_FROM:
                raise ValueError(msg("board.return_status", status=task.status.value))
            self._check_transition(key, Status.IN_PROGRESS, actor)
            message, event = self._append_message(key, actor, text)
            return [event] + self.transition(key, Status.IN_PROGRESS, actor, wake_extra={"message_id": message.id})

    @staticmethod
    def _require_user(actor: str, message: str) -> None:
        if actor not in ACTORS and actor != "project_agent":
            raise ValueError(msg("board.unknown_actor", actor=actor))
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
                raise ValueError(msg("board.fields_not_object"))
            unknown = set(fields) - UPDATABLE_FIELDS
            if unknown:
                raise ValueError(msg("board.unknown_fields", names=", ".join(sorted(map(str, unknown)))))
            owned = set(fields) & RUNNER_FIELDS
            if owned and actor != "system":
                raise PermissionError(msg("board.runner_fields", names=", ".join(sorted(owned))))
            if set(fields) & TRACKER_LINK_FIELDS:
                if actor not in ("user", "system"):
                    raise PermissionError(msg("board.tracker_user_only"))
                _check_tracker_link(fields)
                if fields.get("tracker_key") is not None:
                    check_issue_key(self.store.load_config().tracker, fields["tracker_key"])
            if set(fields) & TASK_TEXT_FIELDS:
                if actor not in ("user", "system"):
                    raise PermissionError(msg("board.wording_user_only"))
                _check_task_text(fields)
            if "rule_violations" in fields:
                if actor not in RULE_VIOLATION_AUTHORS:
                    raise PermissionError(msg("board.violations_actor"))
                fields = dict(fields, rule_violations=_clean_rule_violations(fields["rule_violations"]))
            tasks = self.store.load_tasks()
            task = self._get(tasks, key)
            before = self._ready(tasks)
            for name, value in self._localized(fields).items():
                setattr(task, name, value)
            task.updated_at = self._clock()
            self.store.save_task(task)
            events = [self._emit("task_updated", {"key": key, "fields": sorted(fields)})]
            if "external_blocker" in fields:
                events.extend(self._unblocked(before, [key]))
            return events

    def set_deps(self, key: str, deps: List[str], actor: str) -> List[Event]:
        """Зависимости меняет только пользователь; агенты предлагают изменение через агента проекта."""
        with self._lock:
            self._reject_project_agent(actor)
            self._require_user(actor, msg("board.deps_user_only"))
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
        if choice is not None and not isinstance(choice, str):
            raise ValueError(msg("api.bad_choice"))
        if not isinstance(comment, str):
            raise ValueError(msg("api.bad_comment"))
        with self._lock:
            task = self._get(self.store.load_tasks(), key)
            question = next((q for q in task.questions if q.id == qid), None)
            if question is None:
                raise KeyError(msg("board.unknown_question", id=qid))
            if question.answer is not None:
                raise ValueError(msg("board.question_answered", id=qid))
            now = self._clock()
            question.answer = {"choice": choice, "comment": comment, "at": now}
            task.updated_at = now
            self.store.save_task(task)
            text = "\n".join(p for p in (choice, comment) if p)
            message = Message(id=uuid.uuid4().hex, author="user", text=text, at=now, question_id=qid)
            self.store.append_message(key, message)
            events = [
                self._emit("question_answered", {"key": key, "question_id": qid}),
                self._emit("message_posted", {"thread": key, "message_id": message.id, "author": "user"}),
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
                    raise ValueError(msg("api.bad_from_task"))
                self._get(self.store.load_tasks(), from_task)
            message, posted = self._append_message(thread, author, text)
            events = [posted]
            wakes_agent = author == "user" or (author == "project_agent" and task is not None)
            if author == "user" and task is not None and self.store.load_config().stage == "decomposition":
                # агентов задач до утверждения разбивки нет: комментарий к задаче читает агент проекта
                events.append(self._emit("agent_wake", {"key": PROJECT_THREAD, "reason": "message", "task": thread,
                                                        "message_id": message.id, "by": author}))
            elif wakes_agent:
                events.append(self._emit("agent_wake", {"key": thread, "reason": "message", "message_id": message.id,
                                                        "by": author}))
            elif author == "agent" and from_task:
                events.append(self._emit("agent_wake", {"key": PROJECT_THREAD, "reason": "escalation", "from": from_task,
                                                        "by": author}))
            return events

    def _append_message(self, thread: str, author: str, text: str):
        """Сообщение в тред без пробуждений: (сообщение, событие message_posted)."""
        message = Message(id=uuid.uuid4().hex, author=author, text=self._localized(text), at=self._clock())
        self.store.append_message(thread, message)
        if author != "user":
            if thread != PROJECT_THREAD:
                task = self._get(self.store.load_tasks(), thread)
                task.unread += 1
                self.store.save_task(task)
            else:
                meta = self.store.load_meta()
                meta["project_unread"] += 1
                self.store.save_meta(meta)
        return message, self._emit("message_posted", {"thread": thread, "message_id": message.id, "author": author})

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
            # помечаем применённым заранее, чтобы само действие не сочло предложение устаревшим
            proposal.status = "applied"
            self.store.save_proposals(proposals)
            try:
                if proposal.kind == "add_task":
                    events = self.add_task(new_task_from_dict(proposal.payload["task"]), "user")
                elif proposal.kind == "set_deps":
                    events = self.set_deps(proposal.payload["key"], list(proposal.payload["deps"]), "user")
                else:
                    events = self.transition(proposal.payload["key"], Status.CANCELLED, "user")
            except Exception:
                self._reopen_proposal(pid)
                raise
            events.append(self._emit("proposal_applied", {"id": pid}))
            text = msg("board.applied_proposal", id=pid, change=self._describe_proposal(proposal))
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

    def _reopen_proposal(self, pid: str) -> None:
        proposals = self.store.load_proposals()
        for p in proposals:
            if p.id == pid:
                p.status = "open"
        self.store.save_proposals(proposals)

    def _retire_proposals(self) -> List[Event]:
        """Помечает устаревшими открытые предложения, которые на текущем графе уже не применить."""
        proposals = self.store.load_proposals()
        tasks = self.store.load_tasks()
        keys = {k.lower(): t for k, t in tasks.items()}
        events = []
        for p in proposals:
            if p.status != "open":
                continue
            reason = self._obsolete_reason(p, keys)
            if reason:
                p.status = "obsolete"
                p.obsolete_reason = reason
                events.append(self._emit("proposal_obsolete", {"id": p.id, "reason": reason}))
        if events:
            self.store.save_proposals(proposals)
        return events

    @staticmethod
    def _obsolete_reason(proposal: Proposal, tasks_by_lower_key: Dict[str, Task]) -> str:
        """Почему предложение уже не применить; пустая строка — ещё применимо."""
        payload = proposal.payload if isinstance(proposal.payload, dict) else {}
        if proposal.kind == "add_task":
            task = payload.get("task")
            key = task.get("key") if isinstance(task, dict) else None
            if isinstance(key, str) and key.lower() in tasks_by_lower_key:
                return "exists"
            return ""
        key = payload.get("key")
        if not isinstance(key, str):
            return ""
        task = tasks_by_lower_key.get(key.lower())
        if task is None or task.key != key:
            return "missing"
        if task.status == Status.CANCELLED:
            return "cancelled"
        return ""

    def _check_proposal(self, kind: str, payload: dict) -> str:
        """Проверяет предложение на текущем состоянии графа, возвращает ключ затронутой задачи."""
        if kind not in ("add_task", "set_deps", "cancel_task"):
            raise ValueError(msg("board.unknown_proposal_kind", kind=kind))
        if not isinstance(payload, dict):
            raise ValueError(msg("board.proposal_payload"))
        tasks = self.store.load_tasks()
        if kind == "add_task":
            raw = payload.get("task")
            if not isinstance(raw, dict):
                raise ValueError(msg("board.proposal_task"))
            task = new_task_from_dict(raw)
            self._check_new_key(task.key, tasks)
            self._check_repo(task.repo)
            validate_deps(task.key, task.deps, tasks)
            return task.key
        key = payload.get("key")
        if not isinstance(key, str):
            raise ValueError(msg("board.proposal_key"))
        if kind == "set_deps":
            deps = payload.get("deps")
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ValueError(msg("board.proposal_deps"))
            key = self._get(tasks, key).key
            validate_deps(key, list(deps), tasks)
            return key
        return self._get(tasks, key).key

    @staticmethod
    def _open_proposal(proposals: List[Proposal], pid: str) -> Proposal:
        proposal = next((p for p in proposals if p.id == pid), None)
        if proposal is None:
            raise KeyError(msg("board.unknown_proposal", id=pid))
        if proposal.status != "open":
            raise ValueError(msg("board.proposal_done", id=pid, status=proposal.status))
        return proposal

    @staticmethod
    def _describe_proposal(proposal: Proposal) -> Msg:
        p = proposal.payload
        if proposal.kind == "add_task":
            return msg("board.applied_add_task", key=p["task"]["key"])
        if proposal.kind == "set_deps":
            return msg("board.applied_set_deps", key=p["key"], deps=", ".join(p["deps"]) or msg("board.applied_no_deps"))
        return msg("board.applied_cancel_task", key=p["key"])

    @staticmethod
    def _check_new_key(key: str, tasks) -> None:
        if not isinstance(key, str):
            raise ValueError(msg("board.key_not_str"))
        if key.startswith("_"):
            raise ValueError(msg("board.key_underscore", key=key))
        _check_ident(key, "board.ident_task")
        # файлы задач на регистронезависимой ФС перезаписали бы друг друга
        clash = next((k for k in tasks if k.lower() == key.lower()), None)
        if clash is not None:
            raise ValueError(msg("board.task_exists", key=clash))

    def _check_repo(self, repo: Optional[str]) -> None:
        """Задача ссылается только на репозиторий проекта, если репозитории заданы."""
        names = [r.get("name") for r in self.store.load_config().repos if isinstance(r, dict)]
        if repo and names and repo not in names:
            raise ValueError(msg("board.unknown_repo", repo=repo, names=", ".join(map(str, names))))

    @staticmethod
    def _reject_project_agent(actor: str) -> None:
        if actor == "project_agent":
            raise PermissionError(msg("board.graph_via_proposals"))

    def _emit(self, type: str, data: dict) -> Event:
        event = self.store.append_event(type, self._localized(data), self._clock())
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
            raise KeyError(msg("board.unknown_task", key=key))
        return tasks[key]
