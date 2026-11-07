from __future__ import annotations

import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import prompts, ship, tracker
from .board import RETURNED_FROM, Board
from .graph import base_for, open_deps, ready_tasks
from .harness import (
    Harness,
    TurnResult,
    group_alive,
    is_same_process,
    make_harness,
    terminate,
    signal_group,
    terminate_group,
    turn_process_record,
)
from .messages import LANG_ENV, Msg, msg, normalize, reason as error_text
from .model import DEFAULT_PRIORITY, PRIORITIES, Event, Status, Task
from .review import RoundResult, kill_group, kill_round, make_review, port_of, prune_review_data, trim_log
from .store import PROJECT_THREAD, board_home, list_projects
from .wakes import WakeContext
from .worktree import (
    GitError,
    RebaseOutcome,
    branch_exists,
    carry_ref,
    changed_files,
    commit_all,
    ensure_worktree,
    is_ancestor,
    rebase_carrying_changes,
    uncommit_onto,
    unmerged_files,
    worktree_path,
)

BOARD_CLI = "agent-board"
MAX_FAILURES = 3
STOP_GRACE_SECONDS = 10.0
# паузы перед повтором после 1-й и 2-й подряд неудачи
RETRY_DELAYS = (5.0, 30.0)
PROJECT_SYSTEM_PROMPT = (
    "Ты агент проекта доски agent-board. Не пиши код и не меняй репозитории. "
    "Читай доску и общайся командой agent-board."
)
# сколько подряд пробуждений одного агента могут вызвать другие агенты без участия пользователя
MAX_AGENT_WAKES = 5
AGENT_AUTHORS = ("agent", "project_agent")
# причины, которые не добавляют ничего к стартовому промпту задачи
_START_ONLY = {"ready", "unblocked", "manual", "interrupted", "queued_restart", "rebase"}
# так Claude отвечает на продолжение сессии, которой у него нет (например, первый ход упал до её сохранения)
LOST_SESSION_MARK = "No conversation found"
REVIEW_ERROR_CHARS = 1000

SHIP_KILL_GRACE = 3.0
# как часто раннер убирает устаревшие данные ревью (кроме уборки при старте)
HOUSEKEEPING_SECONDS = 24 * 3600
# как часто проверяется, влиты ли одобренные и отправленные задачи в базовую ветку
MERGE_CHECK_SECONDS = 300
SHIP_TEXT_CHARS = 500
# сколько вывода упавшей команды отправки получает агент
SHIP_WAKE_CHARS = 3000
# причины пробуждения, перед которыми ветка задачи переносится на свежую базу
REBASE_REASONS = ("rebase", "ship_failed")

Key = Tuple[str, str]


def _tail(text, limit: int):
    """Хвост произвольного текста; сообщения каталога короткие и остаются переводимыми."""
    return text if isinstance(text, Msg) else text[-limit:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_board_cli(bin_dir: Path) -> Path:
    """Обёртка agent-board для реальных харнессов: тот же интерпретатор и пакет, что у сервера."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "agent-board"
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    # пакет ищется только в каталоге сервера: одноимённый пакет в текущем каталоге агента его не подменит
    boot = "import runpy, sys; sys.path[0] = sys.argv.pop(1); runpy.run_module('agent_board', run_name='__main__')"
    text = (
        "#!/bin/sh\n"
        "exec %s -c %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(boot), shlex.quote(pkg_parent))
    )
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class _PrepareError(Exception):
    pass


class _ForeignWorktree(GitError):
    """worktree в карточке не совпадает с тем, что раннер создаёт для задачи: git там не трогаем."""


class _Turn:
    def __init__(self, proc, reasons: List[dict], fresh: bool, session_id: Optional[str]):
        self.proc = proc
        self.reasons = reasons
        self.fresh = fresh
        self.session_id = session_id
        self.stopped = False


class _Review:
    def __init__(self, proc, rnd: int, with_session: bool):
        self.proc = proc
        self.round = rnd
        self.with_session = with_session
        self.stopped = False


class _ShipBatch:
    """Очередь отправки одного запроса: задачи идут по одной в порядке зависимостей."""

    def __init__(self, slug: str, keys: List[str]):
        self.slug = slug
        self.queue: Deque[str] = deque(keys)
        self.failed: set = set()


class _Ship:
    def __init__(self, proc, batch: _ShipBatch, log: Path, timeout: float,
                 scrub: Callable[[str], str] = lambda text: text):
        self.proc = proc
        self.batch = batch
        self.log = log
        self.timeout = timeout
        self.scrub = scrub
        self.stopped = False


def _scrubber(config) -> Callable[[str], str]:
    """Маскирует секреты трекера проекта в выводе команды отправки."""
    secrets = tracker.secrets_of(config.tracker)
    return lambda text: tracker.scrub(text, secrets)


class Runner:
    """Превращает события доски в ходы агентов.

    Все реакции и все изменения доски выполняет один рабочий поток строго по порядку событий;
    ходы ждут свои процессы в отдельных потоках и возвращают результат в очередь рабочего потока.
    Накопленные причины пробуждений сохраняются в meta.json и переживают перезапуск.
    """

    def __init__(self, server, clock: Optional[Callable[[], str]] = None):
        self._server = server
        self._clock = clock or _utc_now
        self._inbox: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ctx = WakeContext()
        self._known: set = set()
        self._turns: Dict[Key, _Turn] = {}
        self._pending: Dict[Key, List[dict]] = {}
        self._saved_pending: Dict[str, dict] = {}
        self._waiting: Deque[Key] = deque()
        self._failures: Dict[Key, int] = {}
        self._backoff: Dict[Key, object] = {}
        self._agent_wakes: Dict[Key, int] = {}
        self._parked: set = set()
        self._reviews: Dict[Key, _Review] = {}
        self._ships: Dict[Key, _Ship] = {}
        self._housekeeping_timer: Optional[threading.Timer] = None
        self._merge_timer: Optional[threading.Timer] = None
        self._stopping = threading.Event()

    # --- жизненный цикл ---

    def start(self) -> None:
        self._stopping.clear()
        self._server.hub.subscribe(self.on_event)
        self._inbox.put(("recover",))
        self._thread = threading.Thread(target=self._loop, name="board-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Штатная остановка: процессы идущих ходов завершаются, а на доске ходы остаются идущими,
        чтобы следующий старт продолжил их как прерванные."""
        self._server.hub.unsubscribe(self.on_event)
        self._stopping.set()
        for timer in (self._housekeeping_timer, self._merge_timer):
            if timer is not None:
                timer.cancel()
        self._inbox.put(None)
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        self._kill_reviews()
        self._kill_ships()
        stoppers = [threading.Thread(target=terminate, args=(turn.proc, STOP_GRACE_SECONDS), daemon=True)
                    for turn in list(self._turns.values())]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join(STOP_GRACE_SECONDS + 10)

    def kill_all_for_tests(self, grace: float = 1.0) -> None:
        """Останавливает процессы всех идущих ходов (после stop, чтобы тесты не оставляли агентов)."""
        for turn in list(self._turns.values()):
            terminate(turn.proc, grace)
        self._kill_reviews()
        self._kill_ships()

    def _kill_ships(self) -> None:
        """Отправки останавливаются вместе с сервером; на доске они остаются идущими и после старта помечаются прерванными."""
        ships = list(self._ships.values())
        for sh in ships:
            sh.stopped = True
        stoppers = [threading.Thread(target=terminate, args=(sh.proc, SHIP_KILL_GRACE), daemon=True) for sh in ships]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join(SHIP_KILL_GRACE + 10)

    def _kill_reviews(self) -> None:
        """Клиенты раундов останавливаются без SIGTERM: страница ревью у пользователя остаётся открытой,
        а следующий старт переподключится к ней (на доске раунд остаётся открытым)."""
        for rv in list(self._reviews.values()):
            rv.stopped = True
            kill_round(rv.proc)
            try:
                rv.proc.wait(timeout=5)
            except Exception:
                pass

    def on_event(self, slug: str, event: Event) -> None:
        self._inbox.put(("event", slug, event))

    def sync(self, timeout: float) -> bool:
        """Ждёт, пока рабочий поток обработает всё, что поставлено в очередь до вызова."""
        done = threading.Event()
        self._inbox.put(("sync", done))
        return done.wait(timeout)

    def _loop(self) -> None:
        while True:
            item = self._inbox.get()
            if item is None:
                return
            try:
                self._dispatch(item)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._report_failure(item, e)
            try:
                self._persist_pending()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "event":
            self._on_board_event(item[1], item[2])
        elif kind == "done":
            self._on_turn_done(item[1], item[2], item[3])
        elif kind == "retry":
            self._on_retry(item[1], item[2])
        elif kind == "review_url":
            self._on_review_url(item[1], item[2], item[3], item[4])
        elif kind == "review_done":
            self._on_review_done(item[1], item[2], item[3])
        elif kind == "ship_done":
            self._on_ship_done(item[1], item[2], item[3], item[4])
        elif kind == "recover":
            self._recover()
            self._housekeeping()
            self._check_merged()
        elif kind == "housekeeping":
            self._housekeeping()
        elif kind == "merge_check":
            self._check_merged()
        elif kind == "sync":
            item[1].set()

    def _report_failure(self, item: tuple, error: Exception) -> None:
        if item[0] == "event":
            slug, key = item[1], item[2].data.get("key")
        elif item[0] in ("done", "retry", "review_url", "review_done", "ship_done"):
            slug, key = item[1]
        else:
            return
        try:
            board = self._board(slug)
            if key == PROJECT_THREAD or key in board.store.load_tasks():
                board.post_message(key, "system", msg("run.runner_error", error=repr(error)))
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _persist_pending(self) -> None:
        by_slug: Dict[str, dict] = {slug: {} for slug in self._saved_pending}
        for (slug, key), reasons in self._pending.items():
            if reasons:
                by_slug.setdefault(slug, {})[key] = reasons
        for slug, data in by_slug.items():
            if self._saved_pending.get(slug) != data:
                self._board(slug).update_meta({"runner_pending": data})
                self._saved_pending[slug] = {k: list(v) for k, v in data.items()}

    # --- проекты ---

    def _board(self, slug: str) -> Board:
        board = self._server.board_for(slug)
        if slug not in self._known:
            board.terminal_command_for = lambda t, b=board: self._terminal_command(b, t)
            write_board_cli(board.store.root / "bin")
            self._known.add(slug)
        return board

    def _terminal_command(self, board: Board, task: Task) -> Optional[str]:
        if not task.worktree or not task.session_id:
            return None
        return self._harness(board, task).terminal_command(Path(task.worktree), task.session_id)

    @staticmethod
    def _harness(board: Board, task: Optional[Task] = None) -> Harness:
        project = board.store.load_config()
        config = dict(project.harness)
        if task is not None and task.harness:
            config["type"] = task.harness
        review = project.review if isinstance(project.review, dict) else {}
        if (review.get("tool") or "crit") == "crit":
            rule = "Bash(%s comment:*)" % (review.get("binary") or "crit")
            tools = list(config.get("allowed_tools") or [])
            config["allowed_tools"] = tools if rule in tools else tools + [rule]
        return make_harness(config)

    def _env(self, board: Board, key: str) -> dict:
        project = key == PROJECT_THREAD
        return {
            "AGENT_BOARD_HOME": str(board_home()),
            "AGENT_BOARD_PROJECT": board.store.slug,
            "AGENT_BOARD_ACTOR": "project_agent" if project else "agent",
            "AGENT_BOARD_TASK": "" if project else key,
            # сообщения доски агент получает на языке проекта
            LANG_ENV: normalize(board.store.load_config().language),
            "PATH": str(board.store.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }

    @staticmethod
    def _kill_leftover(board: Board, thread: str, record) -> None:
        # процесс прерванного хода мог пережить сервер: две копии на одной сессии недопустимы
        if not isinstance(record, dict):
            return
        pgid = record.get("pgid")
        if isinstance(pgid, int) and pgid > 0 and not group_alive(pgid):
            # процесса уже нет (например, сервер остановлен штатно): трогать нечего
            return
        if is_same_process(record):
            terminate_group(record["pgid"], STOP_GRACE_SECONDS)
            return
        print("runner: old turn process of %s not found: %r" % (thread, record), file=sys.stderr)
        board.post_message(thread, "system", msg("run.leftover_missing"))

    def _recover(self) -> None:
        for slug in list_projects():
            try:
                board = self._board(slug)
            except (KeyError, ValueError):
                continue
            meta = board.store.load_meta()
            config = board.store.load_config()
            tasks = board.store.load_tasks()
            restored = {}
            for key, reasons in (meta.get("runner_pending") or {}).items():
                if (key == PROJECT_THREAD or key in tasks) and isinstance(reasons, list) and reasons:
                    restored[key] = [r for r in reasons if isinstance(r, dict)]
                    self._pending[(slug, key)] = list(restored[key])
            self._saved_pending[slug] = restored
            woken = set()
            # счётчик переписки агентов не переживает перезапуск — снимаем и остановки
            if meta.get("project_agent_parked"):
                self._update_project_meta(board, {"project_agent_parked": False})
            for key, task in sorted(tasks.items()):
                if isinstance(task.ship, dict) and task.ship.get("state") in ("queued", "running"):
                    self._ship_interrupted(board, task)
            for key, task in sorted(tasks.items()):
                if task.agent_parked:
                    board.update_task(key, "system", {"agent_parked": False})
                if task.agent_state == "running":
                    self._kill_leftover(board, key, task.turn_process)
                    board.update_task(key, "system", {"agent_state": "idle", "turn_process": None})
                    board.post_message(key, "system", msg("run.interrupted"))
                    board.record("agent_wake", {"key": key, "reason": "interrupted"})
                    woken.add(key)
                elif task.agent_state == "waiting_turn":
                    board.update_task(key, "system", {"agent_state": "idle", "wait_reason": ""})
                    reason = "queued_restart" if task.session_id else "ready"
                    board.record("agent_wake", {"key": key, "reason": reason})
                    woken.add(key)
            if meta.get("project_agent_state") == "waiting_turn" and PROJECT_THREAD not in restored:
                # ожидание слота без накопленных пробуждений после перезапуска ждать нечего
                self._update_project_meta(board, {"project_agent_state": "idle", "project_wait_reason": ""})
            if meta.get("project_agent_state") == "running":
                self._kill_leftover(board, PROJECT_THREAD, meta.get("project_turn_process"))
                self._update_project_meta(board, {"project_agent_state": "idle", "project_turn_process": None})
                board.post_message(PROJECT_THREAD, "system", msg("run.interrupted"))
                board.record("agent_wake", {"key": PROJECT_THREAD, "reason": "interrupted"})
                woken.add(PROJECT_THREAD)
            elif config.stage == "work" and not config.project_session_id:
                self._wake(slug, PROJECT_THREAD, {"key": PROJECT_THREAD, "reason": "ready"})
                woken.add(PROJECT_THREAD)
            for key, task in sorted(tasks.items()):
                if isinstance(task.review, dict) and task.review.get("state") == "open":
                    self._kill_leftover_review(task.review.get("process"))
                    self._start_review(slug, key, reconnect=True)
            for key in restored:
                state = meta.get("project_agent_state") if key == PROJECT_THREAD else tasks[key].agent_state
                k = (slug, key)
                # ошибочные ждут действия пользователя, как и до перезапуска
                if key not in woken and state != "error" and k not in self._turns and k not in self._waiting:
                    self._schedule(k)

    # --- реакции на события ---

    def _housekeeping(self) -> None:
        """Удаляет данные ревью давно закрытых задач и назначает следующую уборку."""
        try:
            now = datetime.fromisoformat(self._clock())
        except (TypeError, ValueError):
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        for slug in list_projects():
            try:
                board = self._board(slug)
                busy = {key for (s, key) in self._reviews if s == slug}
                prune_review_data(board.store, now, skip=busy)
            except Exception:
                traceback.print_exc(file=sys.stderr)
        if self._stopping.is_set():
            return
        timer = threading.Timer(HOUSEKEEPING_SECONDS, lambda: self._inbox.put(("housekeeping",)))
        timer.daemon = True
        timer.start()
        self._housekeeping_timer = timer

    def _check_merged(self) -> None:
        """Переводит в «Готово» одобренные и отправленные задачи, уже влитые в базовую ветку репозитория,
        и назначает следующую проверку."""
        for slug in list_projects():
            try:
                self._check_merged_project(self._board(slug))
            except Exception:
                traceback.print_exc(file=sys.stderr)
        if self._stopping.is_set():
            return
        timer = threading.Timer(MERGE_CHECK_SECONDS, lambda: self._inbox.put(("merge_check",)))
        timer.daemon = True
        timer.start()
        self._merge_timer = timer

    def _check_merged_project(self, board: Board) -> None:
        tasks = board.store.load_tasks()
        candidates = [t for t in tasks.values() if t.status in (Status.APPROVED, Status.SENT) and t.kind != "research"
                      and not (isinstance(t.ship, dict) and t.ship.get("state") in ("queued", "running"))]
        if not candidates:
            return
        config = board.store.load_config()
        approvals = self._approval_commits(board)
        for task in sorted(candidates, key=lambda t: t.key):
            try:
                repo, base = self._repo(config, task)
                if self._merged(repo, base, task, approvals.get(task.key)):
                    board.mark_merged(task.key, task.status, base)
            except (_PrepareError, GitError) as e:
                # проверка повторится по расписанию: тред задачи ошибками не засоряем
                print("runner: не удалось проверить влив %s: %s" % (task.key, e), file=sys.stderr)

    @staticmethod
    def _approval_commits(board: Board) -> Dict[str, str]:
        """Последний коммит одобрения каждой задачи."""
        commits: Dict[str, str] = {}
        for event in board.store.read_events():
            if event.type == "task_committed" and event.data.get("key") and event.data.get("sha"):
                commits[str(event.data["key"])] = str(event.data["sha"])
        return commits

    @staticmethod
    def _merged(repo: Path, base: str, task: Task, approval: Optional[str]) -> bool:
        # без коммита одобрения ветка не отличается от базы, и по истории влив не определить
        if not approval or not is_ancestor(repo, approval, base):
            return False
        # после влива ветку часто удаляют: тогда достаточно коммита одобрения
        if task.branch and branch_exists(repo, task.branch):
            return is_ancestor(repo, "refs/heads/" + task.branch, base)
        return True

    def _reset_failures(self, k: Key) -> None:
        self._failures.pop(k, None)
        self._backoff.pop(k, None)

    def _on_board_event(self, slug: str, event: Event) -> None:
        self._ctx.observe(slug, event)
        d = event.data
        if event.type == "status_changed":
            k = (slug, str(d.get("key")))
            if d.get("actor") == "user":
                self._reset_failures(k)
                self._unpark(k)
                if d.get("to") == Status.CANCELLED.value:
                    self._cancel_ship(k)
            if d.get("to") == Status.APPROVED.value and (d.get("actor") == "user" or d.get("auto") is True):
                # коммитится только законченная работа: идущий ход останавливаем до коммита
                self._interrupt_turn(k)
                self._commit(slug, k[1])
            # одобренная задача копила причины без ходов: после возврата из одобрения они доставляются;
            # при возврате в работу следом придёт пробуждение returned, и всё уйдёт одним ходом
            returned = d.get("actor") in ("user", "tracker") and d.get("to") == Status.IN_PROGRESS.value \
                and d.get("from") in {st.value for st in RETURNED_FROM}
            resumes = d.get("actor") == "user" or d.get("from") == Status.APPROVED.value
            if resumes and not returned and self._pending.get(k) and k not in self._turns and k not in self._waiting \
                    and k not in self._backoff:
                self._schedule(k)
            self._review_on_status(slug, d)
        elif event.type == "review_requested":
            self._start_review(slug, str(d.get("key")), reconnect=False)
        elif event.type == "ship_requested":
            self._start_ship(slug, [str(x) for x in d.get("keys") or []], bool(d.get("auto")))
        elif event.type == "stage_changed":
            if d.get("to") == "work":
                self._start_work(slug)
        elif event.type == "priority_changed":
            # идущие ходы не прерываются: новый приоритет действует на очередь
            self._drain()
        elif event.type == "agent_stop":
            self._stop(slug, str(d.get("key")))
        elif event.type == "config_changed":
            # поднятый лимит одновременных ходов сразу освобождает слоты для очереди
            if {"max_agents", "global_max_agents"} & set(d.get("fields") or []):
                self._drain()
        elif event.type == "agent_wake":
            board = self._board(slug)
            reason = self._ctx.enrich(slug, board.store, event)
            key = str(d.get("key"))
            if reason.get("reason") == "retry":
                self._retry_now((slug, key), reason)
                return
            if reason.get("reason") in ("answer", "review") or (
                    reason.get("reason") == "message" and reason.get("author") == "user"):
                self._reset_failures((slug, key))
            if self._park_agent_loop((slug, key), reason):
                return
            self._wake(slug, key, reason)

    def _retry_now(self, k: Key, reason: dict) -> None:
        """Повтор упавшего хода по просьбе пользователя: счётчик неудач сбрасывается, а сообщение о прерывании
        идёт агенту перед причинами, которые прошлый ход не выполнил."""
        self._reset_failures(k)
        self._unpark(k)
        if k[1] != PROJECT_THREAD:
            task = self._board(k[0]).store.load_tasks().get(k[1])
            if task is None or task.status == Status.CANCELLED:
                return
        self._pending[k] = [reason] + [r for r in self._pending.get(k, []) if r.get("reason") != "retry"]
        if k not in self._turns and k not in self._waiting:
            self._schedule(k)

    @staticmethod
    def _by_agent(reason: dict) -> bool:
        return reason.get("reason") in ("message", "escalation", "rebase") and reason.get("by") in AGENT_AUTHORS

    def _unpark(self, k: Key) -> None:
        self._agent_wakes.pop(k, None)
        if k in self._parked:
            self._parked.discard(k)
            self._set_parked(k, False)

    def _set_parked(self, k: Key, parked: bool) -> None:
        """Флаг остановки переписки на карточке — по нему интерфейс показывает, что агент ждёт пользователя."""
        board = self._board(k[0])
        if k[1] == PROJECT_THREAD:
            self._update_project_meta(board, {"project_agent_parked": parked})
        elif k[1] in board.store.load_tasks():
            board.update_task(k[1], "system", {"agent_parked": parked})

    @staticmethod
    def _update_project_meta(board: Board, fields: dict) -> None:
        """Состояние агента проекта живёт в meta без событий: изменение отмечается в журнале, чтобы его увидел UI."""
        def visible(meta: dict) -> dict:
            return {"state": meta.get("project_agent_state") or "idle", "parked": bool(meta.get("project_agent_parked")),
                    "wait_reason": meta.get("project_wait_reason") or ""}

        before = visible(board.store.load_meta())
        board.update_meta(fields)
        after = visible(board.store.load_meta())
        if after != before:
            board.record("project_agent_changed", after)

    def _park_agent_loop(self, k: Key, reason: dict) -> bool:
        """Защита от переписки агентов по кругу: True — пробуждение отложено до действия пользователя."""
        if not self._by_agent(reason):
            self._unpark(k)
            return False
        # несколько сообщений до начала хода дают один ход и считаются одним пробуждением
        if not any(self._by_agent(r) for r in self._pending.get(k, [])):
            self._agent_wakes[k] = self._agent_wakes.get(k, 0) + 1
        if self._agent_wakes.get(k, 0) <= MAX_AGENT_WAKES and k not in self._parked:
            return False
        self._pending.setdefault(k, []).append(reason)
        if k not in self._parked:
            self._parked.add(k)
            self._set_parked(k, True)
            self._board(k[0]).post_message(k[1], "system", msg("run.agents_loop"))
        return True

    def _own_worktree(self, config, task: Task) -> Path:
        try:
            repo, _ = self._repo(config, task)
        except _PrepareError as e:
            raise _ForeignWorktree(msg("run.worktree_check_failed", path=task.worktree, error=error_text(e)))
        expected = worktree_path(repo, task.key)
        if not task.worktree or Path(task.worktree).resolve() != expected.resolve():
            raise _ForeignWorktree(msg("run.worktree_mismatch", path=task.worktree, expected=str(expected)))
        return Path(task.worktree)

    def _commit(self, slug: str, key: str) -> None:
        board = self._board(slug)
        task = board.store.load_tasks().get(key)
        if task is None or not task.worktree:
            return
        try:
            path = self._own_worktree(board.store.load_config(), task)
            sha = commit_all(path, "%s %s" % (key, task.title))
        except GitError as e:
            # без коммита зависимые стартовали бы без кода задачи: возвращаем её на ревью до следующих событий
            board.transition_if(key, Status.APPROVED, Status.IN_REVIEW, "system")
            board.post_message(key, "system", msg("run.commit_failed", error=error_text(e)))
            return
        if sha:
            board.record("task_committed", {"key": key, "sha": sha})
        self._auto_ship(board, task)

    def _auto_ship(self, board: Board, task: Task) -> None:
        """По настройке ship.on_approve закоммиченная одобренная задача сразу встаёт в очередь отправки."""
        if task.kind == "research" or not ship.on_approve(board.store.load_config()):
            return
        try:
            board.request_auto_ship(task.key)
        except ValueError as e:
            current = board.store.load_tasks().get(task.key)
            if current is not None and not ship.is_shipping(current):
                self._ship_set(board, task.key, state="error", at=self._clock(), error=str(e), output_tail="",
                               exit_code=None)
            board.post_message(task.key, "system", msg("run.ship_auto_error", error=error_text(e)))

    def _interrupt_turn(self, k: Key) -> None:
        """Останавливает идущий ход и ждёт конца процесса; накопленные причины остаются."""
        turn = self._turns.get(k)
        if turn is not None and not turn.stopped:
            turn.stopped = True
            terminate(turn.proc, STOP_GRACE_SECONDS)

    def _stop(self, slug: str, key: str) -> None:
        k = (slug, key)
        self._pending.pop(k, None)
        self._backoff.pop(k, None)
        if k in self._waiting:
            self._waiting.remove(k)
            self._set_state(k, "idle")
        turn = self._turns.get(k)
        if turn is not None and not turn.stopped:
            turn.stopped = True
            threading.Thread(target=terminate, args=(turn.proc, STOP_GRACE_SECONDS), daemon=True).start()

    def _wake(self, slug: str, key: str, reason: dict) -> None:
        k = (slug, key)
        if key != PROJECT_THREAD:
            task = self._board(slug).store.load_tasks().get(key)
            if task is None or task.status == Status.CANCELLED:
                return
        turn = self._turns.get(k)
        # «начинай работу» во время стартового хода ничего не добавляет
        if turn is not None and turn.fresh and reason.get("reason") in ("ready", "unblocked", "manual"):
            return
        self._pending.setdefault(k, []).append(reason)
        if k in self._turns or k in self._waiting or k in self._backoff:
            return
        self._schedule(k)

    def _start_work(self, slug: str) -> None:
        """Проект перешёл к работе: запускаются задачи с накопленными пробуждениями и готовые."""
        board = self._board(slug)
        tasks = board.store.load_tasks()
        config = board.store.load_config()
        ready = set(ready_tasks(tasks, config.chains))
        project = (slug, PROJECT_THREAD)
        if not config.project_session_id and project not in self._turns and project not in self._waiting \
                and project not in self._backoff and not self._pending.get(project):
            self._wake(slug, PROJECT_THREAD, {"key": PROJECT_THREAD, "reason": "ready"})
        for key in sorted(tasks):
            k = (slug, key)
            if k in self._turns or k in self._waiting or k in self._backoff:
                continue
            if not self._pending.get(k) and key in ready and not tasks[key].session_id:
                self._pending[k] = [{"key": key, "reason": "ready"}]
            if self._pending.get(k):
                self._schedule(k)

    @staticmethod
    def _on_hold(config, task: Task) -> bool:
        """Агенту задачи ходы не положены: проект ещё не на стадии работы или задача одобрена."""
        return config.stage != "work" or task.status in (Status.APPROVED, Status.SENT)

    # --- очередь, лимиты, повторы ---

    def _schedule(self, k: Key) -> None:
        if self._failures.get(k, 0) >= MAX_FAILURES or k in self._parked:
            return
        if k[1] != PROJECT_THREAD:
            board = self._board(k[0])
            task = board.store.load_tasks().get(k[1])
            # причины остаются в pending до стадии работы или возврата задачи из одобрения
            if task is not None and self._on_hold(board.store.load_config(), task):
                if task.agent_state == "waiting_turn":
                    self._set_state(k, "idle")
                return
        limit = self._limit_reached(k)
        if limit is None:
            self._run(k)
        else:
            self._waiting.append(k)
            self._set_state(k, "waiting_turn", wait_reason=limit)

    def _limit_reached(self, k: Key) -> Optional[str]:
        """Какой лимит одновременных ходов не даёт начать ход: global, project или None, если слот есть."""
        # агент проекта — разговор с пользователем: запускается сверх лимитов и не занимает слоты задач
        if k[1] == PROJECT_THREAD:
            return None
        config = self._board(k[0]).store.load_config()
        task_turns = [s for s, key in self._turns if key != PROJECT_THREAD]
        if len(task_turns) >= config.global_max_agents:
            return "global"
        return None if task_turns.count(k[0]) < config.max_agents else "project"

    def _drain(self) -> None:
        self._sort_waiting()
        for k in list(self._waiting):
            if k in self._parked:
                self._waiting.remove(k)
                self._set_state(k, "idle")
            elif k in self._waiting:
                limit = self._limit_reached(k)
                if limit is None:
                    self._waiting.remove(k)
                    self._run(k)
                elif limit != self._wait_reason(k):
                    self._set_state(k, "waiting_turn", wait_reason=limit)

    def _wait_reason(self, k: Key) -> str:
        board = self._board(k[0])
        if k[1] == PROJECT_THREAD:
            return board.store.load_meta().get("project_wait_reason") or ""
        task = board.store.load_tasks().get(k[1])
        return task.wait_reason if task is not None else ""

    def _sort_waiting(self) -> None:
        """Очередь на свободный слот: сначала по приоритету задачи, внутри приоритета — по порядку пробуждения."""
        tasks_by_slug: Dict[str, Dict[str, Task]] = {}

        def rank(k: Key) -> int:
            if k[0] not in tasks_by_slug:
                tasks_by_slug[k[0]] = self._board(k[0]).store.load_tasks()
            task = tasks_by_slug[k[0]].get(k[1])
            priority = task.priority if task is not None else DEFAULT_PRIORITY
            return PRIORITIES.index(priority) if priority in PRIORITIES else PRIORITIES.index(DEFAULT_PRIORITY)

        self._waiting = deque(sorted(self._waiting, key=rank))

    def _retry_later(self, k: Key) -> None:
        delays = RETRY_DELAYS or (0.0,)
        delay = delays[min(self._failures.get(k, 1), len(delays)) - 1]
        token = object()
        self._backoff[k] = token
        timer = threading.Timer(delay, self._inbox.put, args=(("retry", k, token),))
        timer.daemon = True
        timer.start()

    def _on_retry(self, k: Key, token: object) -> None:
        if self._backoff.get(k) is not token:
            return
        del self._backoff[k]
        if self._pending.get(k) and k not in self._turns and k not in self._waiting:
            self._schedule(k)

    def _set_state(self, k: Key, state: str, error: Optional[str] = None, wait_reason: str = "") -> None:
        board = self._board(k[0])
        if k[1] == PROJECT_THREAD:
            fields = {"project_agent_state": state, "project_wait_reason": wait_reason}
            if error is not None:
                fields["project_turn_error"] = error
            self._update_project_meta(board, fields)
            return
        if k[1] not in board.store.load_tasks():
            return
        fields = {"agent_state": state, "wait_reason": wait_reason}
        if error is not None:
            fields["turn_error"] = error
        board.update_task(k[1], "system", fields)

    # --- ход ---

    def _run(self, k: Key) -> None:
        try:
            if k[1] == PROJECT_THREAD:
                self._run_project(k)
            else:
                self._run_task(k)
        except Exception as e:
            # ход не начат: причины остаются в pending до следующего пробуждения
            if not isinstance(e, _PrepareError):
                traceback.print_exc(file=sys.stderr)
            if k in self._turns:
                return
            message = error_text(e) if isinstance(e, _PrepareError) else msg("run.internal_error", error=repr(e))
            try:
                self._set_state(k, "error", message)
                self._board(k[0]).post_message(k[1], "system", msg("run.not_started", error=message))
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def _run_task(self, k: Key) -> None:
        slug, key = k
        board = self._board(slug)
        tasks = board.store.load_tasks()
        task = tasks.get(key)
        if task is None or task.status == Status.CANCELLED:
            self._pending.pop(k, None)
            self._set_state(k, "idle")
            return
        config = board.store.load_config()
        reasons = self._pending.get(k, [])
        blocked = bool(task.external_blocker or open_deps(task, tasks, config.chains))
        # заблокированная задача не стартует сама и не ребейзится, причины ждут разблокировки
        if self._on_hold(config, task):
            deliver = []
        elif task.session_id:
            deliver = [r for r in reasons if not (blocked and r.get("reason") == "rebase")]
        else:
            deliver = [] if blocked and task.status == Status.NOT_STARTED else list(reasons)
        if not deliver:
            if task.agent_state == "waiting_turn":
                self._set_state(k, "idle")
            return
        if task.session_id and task.worktree:
            self._resume_task(k, board, task, tasks, config, deliver)
        elif task.session_id:
            raise _PrepareError(msg("run.no_worktree"))
        else:
            self._start_task(k, board, task, tasks, config, deliver)

    def _repo(self, config, task: Task) -> Tuple[Path, str]:
        repos = [r for r in config.repos if isinstance(r, dict) and r.get("path")]
        if task.repo:
            repo = next((r for r in repos if r.get("name") == task.repo), None)
            if repo is None:
                raise _PrepareError(msg("run.repo_missing", repo=task.repo))
        elif len(repos) == 1:
            repo = repos[0]
        else:
            raise _PrepareError(msg("run.repo_ambiguous", count=len(repos)))
        return Path(repo["path"]), repo.get("base") or "main"

    def _start_task(self, k: Key, board: Board, task: Task, tasks, config, deliver: List[dict]) -> None:
        key = task.key
        repo, default_base = self._repo(config, task)
        base, merges = base_for(task, tasks, config.chains, default_base)
        branch = config.branch_template.format(key=key)
        conflicts: List[str] = []
        try:
            path = ensure_worktree(repo, key, branch, base, merges)
        except GitError as e:
            path = worktree_path(repo, key)
            try:
                conflicts = unmerged_files(path) if path.exists() else []
            except GitError:
                conflicts = []
            if not conflicts:
                raise _PrepareError(msg("run.worktree_failed", error=error_text(e)))
        harness = self._harness(board)
        session_id = harness.new_session_id()
        board.update_task(key, "system", {
            "worktree": str(path), "branch": branch, "base_branch": base, "harness": config.harness.get("type", "claude"),
        })
        system = prompts.task_system(config, BOARD_CLI)
        text = prompts.task_prompt(task, config, path, base, BOARD_CLI)
        if not hasattr(harness, "system_prompt"):
            text = system + "\n" + text
        else:
            harness.system_prompt = system
        if conflicts:
            text += ("\nMerging the dependency branches %s left conflict markers in: %s. "
                     "Resolve them by editing the files; do not commit.\n" % (", ".join(merges), ", ".join(conflicts)))
        extra = [r for r in deliver if r.get("reason") not in _START_ONLY]
        if extra:
            text += "\n" + prompts.wake_message(extra, "", BOARD_CLI)
        proc = harness.start_turn(path, text, session_id, self._log(board, key), self._env(board, key))
        self._begin(k, board, harness, proc, _Turn(proc, deliver, True, session_id), deliver,
                    lambda: board.update_task(key, "system", {"session_id": session_id}))

    def _resume_task(self, k: Key, board: Board, task: Task, tasks, config, deliver: List[dict]) -> None:
        try:
            path = self._own_worktree(config, task)
        except _ForeignWorktree as e:
            raise _PrepareError(error_text(e))
        reasons = deliver
        rebases = [r for r in deliver if r.get("reason") in REBASE_REASONS]
        if rebases:
            # несколько переносов подряд сводятся к одному — на актуальную базу; ошибки отправки агент получает все
            last = rebases[-1]
            moved = self._rebase(board, task, tasks, config, last)
            reasons = [r for r in deliver if r.get("reason") != "rebase" and r is not last] + [moved]
        harness = self._harness(board, task)
        if hasattr(harness, "system_prompt"):
            harness.system_prompt = prompts.task_system(config, BOARD_CLI)
        message = prompts.wake_message(reasons, "", BOARD_CLI)
        proc = harness.resume_turn(path, task.session_id, message, self._log(board, task.key), self._env(board, task.key))
        self._begin(k, board, harness, proc, _Turn(proc, reasons, False, task.session_id), deliver)

    def _rebase(self, board: Board, task: Task, tasks, config, reason: dict) -> dict:
        reason = dict(reason)
        try:
            _, default_base = self._repo(config, task)
        except _PrepareError:
            default_base = task.base_branch or "main"
        base, merges = base_for(task, tasks, config.chains, default_base)
        if merges:
            reason.update(base=base, rebase_ok=False, merge_branches=list(merges))
            return reason
        try:
            path = self._own_worktree(config, task)
            outcome = rebase_carrying_changes(path, base, carry_ref(task.key))
            if reason.get("reason") == "ship_failed" and not outcome.ok and not outcome.carried_ref:
                # отправка упала на конфликте с базой: без коммитов перенос оставит конфликты маркерами в файлах
                head = uncommit_onto(path, base)
                if head:
                    board.post_message(task.key, "system", msg("run.ship_uncommitted", base=base, head=head[:10]))
                    outcome = rebase_carrying_changes(path, base, carry_ref(task.key))
        except GitError as e:
            board.post_message(task.key, "system", msg("run.rebase_failed", base=base, error=error_text(e)))
            outcome = RebaseOutcome(False)
        ok = outcome.ok
        reason.update(base=base, rebase_ok=ok, conflict_files=outcome.conflict_files)
        if outcome.carried_ref:
            reason["carried_ref"] = outcome.carried_ref
            ref = outcome.carried_ref
            board.post_message(task.key, "system", msg("run.carried", base=base, ref=ref))
        if ok and task.base_branch != base:
            board.update_task(task.key, "system", {"base_branch": base})
        return reason

    def _run_project(self, k: Key) -> None:
        board = self._board(k[0])
        config = board.store.load_config()
        reasons = list(self._pending.get(k, []))
        cwd = board.store.root / "project-agent"
        cwd.mkdir(parents=True, exist_ok=True)
        harness = self._harness(board)
        if hasattr(harness, "system_prompt"):
            harness.system_prompt = PROJECT_SYSTEM_PROMPT
        log, env = self._log(board, PROJECT_THREAD), self._env(board, PROJECT_THREAD)
        last_seq = board.store.last_seq()
        if config.project_session_id:
            since = int(board.store.load_meta().get("project_last_seen") or 0)
            message = prompts.wake_message(reasons, prompts.digest(board.store.read_events(since)), BOARD_CLI)
            proc = harness.resume_turn(cwd, config.project_session_id, message, log, env)
            turn = _Turn(proc, reasons, False, config.project_session_id)
        else:
            session_id = harness.new_session_id()
            text = prompts.project_prompt(config, board.snapshot(), BOARD_CLI)
            extra = [r for r in reasons if r.get("reason") not in _START_ONLY]
            if extra:
                text += "\n" + prompts.wake_message(extra, "", BOARD_CLI)
            proc = harness.start_turn(cwd, text, session_id, log, env)
            turn = _Turn(proc, reasons, True, session_id)
        self._begin(k, board, harness, proc, turn, reasons, lambda: self._project_started(board, turn, last_seq))

    @staticmethod
    def _project_started(board: Board, turn: _Turn, last_seq: int) -> None:
        if turn.fresh:
            board.update_config({"project_session_id": turn.session_id})
        board.update_meta({"project_last_seen": last_seq})

    @staticmethod
    def _log(board: Board, key: str) -> Path:
        return board.store.root / "logs" / (key + ".jsonl")

    def _begin(self, k: Key, board: Board, harness: Harness, proc, turn: _Turn, delivered: List[dict],
               on_started: Optional[Callable[[], None]] = None) -> None:
        log = self._log(board, k[1])

        def wait() -> None:
            try:
                result = harness.finish(proc, log)
            except Exception as e:
                result = TurnResult(session_id=turn.session_id, ok=False, error=msg("run.internal_error", error=repr(e)), exit_code=-1)
            self._inbox.put(("done", k, turn, result))

        # процесс уже запущен: при любом сбое до передачи хода ожидающему потоку его нужно остановить,
        # иначе он останется без присмотра, а повтор запустит второй на той же сессии
        try:
            if on_started is not None:
                on_started()
            threading.Thread(target=wait, name="board-turn-%s" % k[1], daemon=True).start()
        except BaseException:
            terminate(proc, 0)
            raise
        self._turns[k] = turn
        self._pending[k] = [r for r in self._pending.get(k, []) if not any(r is x for x in delivered)]
        record = turn_process_record(proc, turn.session_id)
        if k[1] == PROJECT_THREAD:
            self._update_project_meta(board, {"project_agent_state": "running", "project_turn_error": "",
                                              "project_wait_reason": "", "project_turn_process": record})
        else:
            board.update_task(k[1], "system", {"agent_state": "running", "turn_error": "", "wait_reason": "",
                                               "turn_process": record})
            if any(r.get("reason") == "review" for r in delivered):
                self._set_review(board, k[1], state="fixing", only_from=("changes",))
        board.record("turn_started", {"key": k[1]})

    def _on_turn_done(self, k: Key, turn: _Turn, result: TurnResult) -> None:
        if self._turns.get(k) is not turn:
            return
        del self._turns[k]
        try:
            self._record_result(k, turn, result)
        finally:
            if self._pending.get(k) and k not in self._waiting and k not in self._backoff:
                if self._failures.get(k) and not turn.stopped and not result.ok:
                    if self._failures[k] < MAX_FAILURES:
                        self._retry_later(k)
                else:
                    self._schedule(k)
            self._drain()
            self._maybe_next_round(k)

    def _record_result(self, k: Key, turn: _Turn, result: TurnResult) -> None:
        board = self._board(k[0])
        project = k[1] == PROJECT_THREAD
        if turn.stopped:
            state, error, event = "idle", "", ("turn_finished", {"key": k[1], "stopped": True})
            session_id = result.session_id or turn.session_id
        elif not result.ok and not turn.fresh and LOST_SESSION_MARK in (result.error or ""):
            # продолжить нечего: следующий ход начнётся новой сессией, это не считается сбоем агента
            state, error, event = "idle", "", ("turn_finished", {"key": k[1], "session_lost": True})
            session_id = None
            self._pending[k] = turn.reasons + self._pending.get(k, [])
            board.post_message(k[1], "system", msg("run.lost_session", session=turn.session_id))
        elif result.ok:
            self._failures.pop(k, None)
            state, error, event = "idle", "", ("turn_finished", {"key": k[1]})
            session_id = result.session_id or turn.session_id
        else:
            self._failures[k] = self._failures.get(k, 0) + 1
            error = result.error or msg("run.turn_failed")
            state, event = "error", ("turn_failed", {"key": k[1], "error": error})
            # неудачный первый ход начнётся заново новой сессией, причины вернутся в следующий ход
            session_id = None if turn.fresh else (result.session_id or turn.session_id)
            self._pending[k] = turn.reasons + self._pending.get(k, [])
        if project:
            board.update_config({"project_session_id": session_id})
            self._update_project_meta(board, {"project_agent_state": state, "project_turn_error": error,
                                              "project_turn_process": None})
        elif k[1] in board.store.load_tasks():
            self._update_task_after_turn(board, k[1], state, error, session_id)
        board.record(event[0], event[1])
        if state == "error" and self._failures.get(k, 0) >= MAX_FAILURES:
            board.post_message(k[1], "system", msg("run.failures", count=MAX_FAILURES, error=error, key=k[1]))

    def _update_task_after_turn(self, board: Board, key: str, state: str, error: str, session_id: Optional[str]) -> None:
        task = board.store.load_tasks()[key]
        fields = {"agent_state": state, "turn_error": error, "last_turn_at": self._clock(),
                  "session_id": session_id, "turn_process": None}
        if task.worktree and task.base_branch:
            try:
                fields["changed_files"] = changed_files(Path(task.worktree), task.base_branch)
            except GitError:
                pass
        board.update_task(key, "system", fields)

    # --- ревью ---

    @staticmethod
    def _crit_dir(board: Board) -> Path:
        return board.store.root / "crit"

    @staticmethod
    def _set_review(board: Board, key: str, only_from=None, **fields) -> None:
        task = board.store.load_tasks().get(key)
        if task is None:
            return
        state = dict(task.review) if isinstance(task.review, dict) else {}
        if only_from is not None and state.get("state") not in only_from:
            return
        new = dict(state, **fields)
        if new != state:
            board.update_task(key, "system", {"review": new})

    def _start_review(self, slug: str, key: str, reconnect: bool, use_session: bool = True) -> None:
        """Открывает раунд ревью (reconnect — продолжает открытый раунд после перезапуска сервера)."""
        k = (slug, key)
        if k in self._reviews:
            return
        board = self._board(slug)
        task = board.store.load_tasks().get(key)
        if task is None:
            return
        try:
            config = board.store.load_config()
            reviewer = make_review(config.review)
            if not reviewer.interactive:
                if isinstance(task.review, dict) and task.review.get("state") == "open":
                    text = msg("run.review_tool_off", round=int(task.review.get("round") or 0))
                    self._set_review(board, key, state="idle", process=None, error=text)
                    board.post_message(key, "system", text)
                return
            if task.status != Status.IN_REVIEW:
                self._set_review(board, key, state="idle", process=None)
                return
            state = dict(task.review) if isinstance(task.review, dict) else {}
            rnd = max(int(state.get("round") or 0) + (0 if reconnect else 1), 1)
            path = self._own_worktree(config, task)
            session = state.get("session") if use_session else None
            base = task.base_branch or self._repo(config, task)[1]
            proc = reviewer.start(path, base, self._crit_dir(board), session=session,
                                  port=port_of(state.get("url")))
        except Exception as e:
            if not isinstance(e, (ValueError, GitError)):
                traceback.print_exc(file=sys.stderr)
            self._review_error(board, key, error_text(e) or repr(e))
            return
        if not proc.pid:
            self._review_error(board, key, proc.error)
            return
        rv = _Review(proc, rnd, bool(session))
        log = board.store.root / "logs" / (key + ".review.log")
        trim_log(log)

        def watch() -> None:
            try:
                result = reviewer.watch(proc, log, lambda url, sid: self._inbox.put(("review_url", k, rv, url, sid)))
            except Exception as e:
                result = RoundResult("failed", error=msg("run.internal_error", error=repr(e)))
            self._inbox.put(("review_done", k, rv, result))

        try:
            threading.Thread(target=watch, name="board-review-%s" % key, daemon=True).start()
        except BaseException:
            kill_round(proc)
            raise
        self._reviews[k] = rv
        state.setdefault("url", None)
        state.setdefault("session", None)
        state.setdefault("history", [])
        state.update(round=rnd, state="open", error="",
                     process=turn_process_record(proc, str(self._crit_dir(board))))
        board.update_task(key, "system", {"review": state})

    def _review_error(self, board: Board, key: str, error: str) -> None:
        error = _tail(error, REVIEW_ERROR_CHARS)
        self._set_review(board, key, state="idle", error=error, process=None)
        board.post_message(key, "system", msg("run.review_failed", error=error))

    @staticmethod
    def _kill_leftover_review(record) -> None:
        # клиент раунда мог пережить упавший сервер: второй клиент на той же сессии недопустим
        if isinstance(record, dict) and is_same_process(record):
            kill_group(record["pgid"])
            for _ in range(100):
                if not group_alive(record["pgid"]):
                    return
                threading.Event().wait(0.05)

    def _close_review(self, k: Key) -> None:
        """Ревью закончено насовсем: останавливается клиент раунда и закрывается страница crit."""
        rv = self._reviews.pop(k, None)
        if rv is not None:
            rv.stopped = True
        board = self._board(k[0])
        task = board.store.load_tasks().get(k[1])
        session = task.review.get("session") if task is not None and isinstance(task.review, dict) else None
        stop_page = None
        if session:
            try:
                config = board.store.load_config()
                reviewer = make_review(config.review)
                path = self._own_worktree(config, task)
            except Exception:
                reviewer = path = None
            if getattr(reviewer, "interactive", False):
                stop_page = (reviewer, path, self._crit_dir(board), session)
        if rv is None and stop_page is None:
            return

        def close() -> None:
            if rv is not None:
                terminate(rv.proc, STOP_GRACE_SECONDS)
            if stop_page is not None:
                reviewer, path, data_dir, sid = stop_page
                reviewer.stop_daemon(path, data_dir, sid)

        threading.Thread(target=close, name="board-review-close-%s" % k[1], daemon=True).start()

    def _review_on_status(self, slug: str, d: dict) -> None:
        """Задача ушла с ревью (одобрена, отменена или возвращена пользователем): раунд закрывается."""
        to = d.get("to")
        ended = to in (Status.APPROVED.value, Status.CANCELLED.value) or (
            d.get("actor") in ("user", "tracker") and to != Status.IN_REVIEW.value)
        if not ended:
            return
        k = (slug, str(d.get("key")))
        self._close_review(k)
        if to == Status.APPROVED.value and self._pending.get(k):
            # замечания одобренного ревью агенту больше не нужны
            self._pending[k] = [r for r in self._pending[k] if r.get("reason") != "review"]
        board = self._board(slug)
        task = board.store.load_tasks().get(k[1])
        if task is None or not isinstance(task.review, dict):
            return
        self._set_review(board, k[1], state="approved" if to == Status.APPROVED.value else "idle", process=None)

    def _on_review_url(self, k: Key, rv: _Review, url: str, session: str) -> None:
        if self._reviews.get(k) is rv:
            self._set_review(self._board(k[0]), k[1], url=url, session=session)

    def _on_review_done(self, k: Key, rv: _Review, result: RoundResult) -> None:
        if self._reviews.get(k) is not rv:
            return
        del self._reviews[k]
        if rv.stopped:
            return
        slug, key = k
        board = self._board(slug)
        task = board.store.load_tasks().get(key)
        if task is None:
            return
        state = dict(task.review) if isinstance(task.review, dict) else {}
        state["process"] = None
        if result.url:
            state["url"] = result.url
        if result.session:
            state["session"] = result.session
        if result.outcome == "no_session" and rv.with_session:
            # страницы ревью больше нет: тот же раунд открывается заново, комментарии crit хранит на диске
            board.update_task(key, "system", {"review": state})
            self._start_review(slug, key, reconnect=True, use_session=False)
            return
        if result.outcome in ("approved", "changes"):
            state["history"] = list(state.get("history") or []) + [
                {"round": rv.round, "state": result.outcome, "comments": len(result.comments), "at": self._clock()}]
        if result.outcome == "approved" and k in self._turns:
            # раунд мог завершить сам агент: одобрение во время его хода оставляем пользователю
            state.update(state="idle", error=msg("run.review_during_turn"))
            board.update_task(key, "system", {"review": state})
            board.post_message(key, "system", msg("run.review_during_turn"))
        elif result.outcome == "approved" and task.status != Status.IN_REVIEW:
            text = msg("run.review_not_in_review", round=rv.round, status=task.status.value)
            state.update(state="idle", error=text)
            board.update_task(key, "system", {"review": state})
            board.post_message(key, "system", text)
        elif result.outcome == "approved":
            state.update(state="approved", error="")
            board.update_task(key, "system", {"review": state})
            # завершение раунда без замечаний — решение самого пользователя в crit
            try:
                board.approve(key, "user", msg("run.review_approved", round=rv.round))
            except ValueError as e:
                self._set_review(board, key, state="idle", error=error_text(e))
                board.post_message(key, "system", msg("run.review_not_in_review", round=rv.round, status=error_text(e)))
        elif result.outcome == "changes":
            state.update(state="changes", error="")
            board.update_task(key, "system", {"review": state})
            reviewer = make_review(board.store.load_config().review)
            board.record("agent_wake", {
                "key": key, "reason": "review", "round": rv.round, "comments": result.comments,
                "binary": getattr(reviewer, "binary", "crit"), "crit_dir": str(self._crit_dir(board))})
        else:
            error = _tail(result.error or result.outcome, REVIEW_ERROR_CHARS)
            state.update(state="idle", error=error)
            board.update_task(key, "system", {"review": state})
            board.post_message(key, "system", msg("run.review_aborted", round=rv.round, error=error))

    def _maybe_next_round(self, k: Key) -> None:
        """Агент закончил правки по ревью: раннер открывает следующий раунд."""
        if k[1] == PROJECT_THREAD or k in self._turns or k in self._waiting or k in self._backoff \
                or self._pending.get(k) or k in self._reviews:
            return
        task = self._board(k[0]).store.load_tasks().get(k[1])
        if task is None or task.status != Status.IN_REVIEW or task.agent_state == "error" \
                or not isinstance(task.review, dict) or task.review.get("state") != "fixing":
            return
        self._start_review(k[0], k[1], reconnect=False)

    # --- отправка в ревью ---

    @staticmethod
    def _ship_interrupted(board: Board, task: Task) -> None:
        state = dict(task.ship)
        if state.get("state") == "running":
            Runner._kill_leftover(board, task.key, state.get("process"))
            tail = ship.tail_of(board.store.root / "logs" / (task.key + ".ship.log"))
            state["output_tail"] = _scrubber(board.store.load_config())(tail)
        was_running = state.get("state") == "running"
        state.update(state="interrupted", process=None)
        board.update_task(task.key, "system", {"ship": state})
        board.post_message(task.key, "system", msg("run.ship_interrupted" if was_running else "run.ship_not_started"))

    def _cancel_ship(self, k: Key) -> None:
        """Отмена задачи останавливает идущую отправку (группу процессов команды)."""
        sh = self._ships.pop(k, None)
        if sh is None:
            return
        sh.stopped = True
        threading.Thread(target=terminate, args=(sh.proc, SHIP_KILL_GRACE), daemon=True).start()
        board = self._board(k[0])
        self._ship_set(board, k[1], state="cancelled", at=self._clock(), output_tail=sh.scrub(ship.tail_of(sh.log)),
                       exit_code=None, process=None)
        board.post_message(k[1], "system", msg("run.ship_cancelled"))
        self._ship_next(sh.batch)

    def _start_ship(self, slug: str, keys: List[str], auto: bool = False) -> None:
        if auto:
            batch = self._dep_batch(slug, keys)
            if batch is not None:
                # зависимость ещё отправляется: задача уйдёт следом в той же очереди и будет пропущена при её ошибке
                batch.queue.extend(keys)
                return
        self._ship_next(_ShipBatch(slug, keys))

    def _dep_batch(self, slug: str, keys: List[str]) -> Optional[_ShipBatch]:
        """Идущая очередь отправки, в которой есть зависимость одной из задач."""
        tasks = self._board(slug).store.load_tasks()
        deps = {d for key in keys if key in tasks for d in tasks[key].deps}
        for (s, key), sh in self._ships.items():
            if s == slug and (key in deps or deps & set(sh.batch.queue)):
                return sh.batch
        return None

    def _ship_next(self, batch: _ShipBatch) -> None:
        while batch.queue:
            key = batch.queue.popleft()
            try:
                started = self._ship_task(batch, key)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._ship_fail(batch, key, repr(e))
                started = False
            if started:
                return

    def _ship_set(self, board: Board, key: str, **state) -> None:
        board.update_task(key, "system", {"ship": state})

    def _ship_fail(self, batch: _ShipBatch, key: str, error: str) -> None:
        """Задача не отправлена по причине вне процесса команды: ошибка на карточке и в треде."""
        board = self._board(batch.slug)
        batch.failed.add(key)
        self._ship_set(board, key, state="error", at=self._clock(), error=error, output_tail="", exit_code=None)
        board.post_message(key, "system", msg("run.ship_error", error=error))

    def _ship_skip(self, batch: _ShipBatch, key: str, reason: str) -> None:
        batch.failed.add(key)
        self._ship_set(self._board(batch.slug), key, state="skipped", at=self._clock(), reason=reason)

    @staticmethod
    def _ship_base(config, task: Task, tasks: Dict[str, Task], default_base: str) -> str:
        if not config.chains:
            return default_base
        unmerged = [tasks[d] for d in task.deps if d in tasks and tasks[d].status in (Status.APPROVED, Status.SENT)]
        if len(unmerged) > 1:
            raise ValueError(msg("run.ship_multi_deps"))
        if not unmerged:
            return default_base
        if not unmerged[0].branch:
            raise ValueError(msg("run.ship_dep_no_branch", key=unmerged[0].key))
        return unmerged[0].branch

    def _ship_task(self, batch: _ShipBatch, key: str) -> bool:
        """Готовит и запускает команду отправки; False — задача завершена без запуска (ошибка или пропуск)."""
        slug = batch.slug
        board = self._board(slug)
        tasks = board.store.load_tasks()
        task = tasks.get(key)
        if task is None:
            batch.failed.add(key)
            return False
        # одобренная, но не отправленная зависимость: её отправка не удалась или не запускалась
        blockers = sorted(d for d in task.deps if d in batch.failed
                          or (d in tasks and tasks[d].status == Status.APPROVED))
        if blockers:
            self._ship_skip(batch, key, msg("run.ship_dep_failed", keys=", ".join(blockers)))
            return False
        if task.status != Status.APPROVED:
            self._ship_skip(batch, key, msg("run.ship_not_approved", status=task.status.value))
            return False
        try:
            config = board.store.load_config()
            command, timeout = ship.settings(config)
            path = self._own_worktree(config, task)
            repo, default_base = self._repo(config, task)
            base = self._ship_base(config, task, tasks, default_base)
            if not task.branch:
                raise ValueError(msg("run.ship_no_branch"))
            rendered = ship.render(command, {
                "key": key, "title": task.title, "branch": task.branch, "base": base,
                "worktree": str(path), "repo": str(repo),
                "tracker_key": task.tracker_key or "", "tracker_url": task.tracker_url or ""})
            sha = commit_all(path, "%s %s" % (key, task.title))
            if sha:
                board.record("task_committed", {"key": key, "sha": sha})
                board.post_message(key, "system", msg("run.ship_committed", sha=sha[:10]))
        except (ValueError, GitError, _PrepareError) as e:
            self._ship_fail(batch, key, error_text(e))
            return False
        log = board.store.root / "logs" / (key + ".ship.log")
        scrub = _scrubber(config)
        try:
            proc = ship.start(rendered, path, log, key, scrub=scrub, extra_env=ship.tracker_env(task))
        except OSError as e:
            self._ship_fail(batch, key, msg("run.ship_start_failed", error=error_text(e)))
            return False
        sh = _Ship(proc, batch, log, timeout, scrub)
        k = (slug, key)

        def watch() -> None:
            timed_out = False
            code: Optional[int] = None
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate(proc, SHIP_KILL_GRACE)
                code = proc.poll()
            except Exception:
                traceback.print_exc(file=sys.stderr)
            # после команды в группе ничего не остаётся: фоновые потомки не переживают отправку
            signal_group(proc, signal.SIGKILL)
            ship.finish_output(proc)
            self._inbox.put(("ship_done", k, sh, code, timed_out))

        try:
            threading.Thread(target=watch, name="board-ship-%s" % key, daemon=True).start()
        except BaseException:
            terminate(proc, SHIP_KILL_GRACE)
            raise
        self._ships[k] = sh
        self._ship_set(board, key, state="running", at=self._clock(), output_tail="", exit_code=None,
                       process=turn_process_record(proc, None))
        board.record("ship_started", {"key": key})
        return True

    def _return_after_ship(self, board: Board, key: str, code: int, tail: str) -> None:
        """По настройке ship.on_failure=return упавшая отправка возвращает задачу агенту: он переносит ветку
        на свежую базу, разбирается с ошибкой и снова отдаёт задачу на ревью."""
        if not ship.returns_on_failure(board.store.load_config()):
            return
        if not board.transition_if(key, Status.APPROVED, Status.IN_PROGRESS, "system"):
            return
        board.post_message(key, "system", msg("run.ship_returned"))
        board.record("agent_wake", {"key": key, "reason": "ship_failed", "exit_code": code,
                                    "output": tail.strip()[-SHIP_WAKE_CHARS:]})

    def _on_ship_done(self, k: Key, sh: _Ship, code: Optional[int], timed_out: bool) -> None:
        if self._ships.get(k) is not sh:
            return
        del self._ships[k]
        if sh.stopped:
            return
        slug, key = k
        board = self._board(slug)
        tail = sh.scrub(ship.tail_of(sh.log))
        ok = code == 0 and not timed_out
        done = {"state": "done", "at": self._clock(), "output_tail": tail, "exit_code": 0, "process": None}
        if ok and not board.transition_if(key, Status.APPROVED, Status.SENT, "system", fields={"ship": done}):
            ok = False
            error = msg("run.ship_status_changed")
            sh.batch.failed.add(key)
            self._ship_set(board, key, state="error", at=self._clock(), error=error, output_tail=tail,
                           exit_code=0, process=None)
            board.post_message(key, "system", msg("run.ship_error", error=error))
            board.record("ship_finished", {"key": key, "ok": False, "exit_code": 0})
        elif ok:
            board.record("ship_finished", {"key": key, "ok": True, "exit_code": 0})
        else:
            sh.batch.failed.add(key)
            exit_code = None if timed_out else code
            self._ship_set(board, key, state="error", at=self._clock(), output_tail=tail, exit_code=exit_code,
                           process=None)
            output = "\n" + tail.strip()[-SHIP_TEXT_CHARS:] if tail.strip() else ""
            text = (msg("run.ship_timeout", seconds=int(sh.timeout), output=output) if timed_out
                    else msg("run.ship_failed", code=code, output=output))
            board.post_message(key, "system", text)
            board.record("ship_finished", {"key": key, "ok": False, "exit_code": exit_code})
            if exit_code is not None:
                self._return_after_ship(board, key, exit_code, tail)
        self._ship_next(sh.batch)
