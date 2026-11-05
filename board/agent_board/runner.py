from __future__ import annotations

import os
import queue
import shlex
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import prompts
from .board import Board
from .graph import base_for, open_deps
from .harness import Harness, TurnResult, is_same_process, make_harness, terminate, terminate_group, turn_process_record
from .model import Event, Status, Task
from .store import PROJECT_THREAD, board_home, list_projects
from .wakes import WakeContext
from .worktree import (
    GitError,
    changed_files,
    commit_all,
    ensure_worktree,
    rebase_carrying_changes,
    unmerged_files,
    worktree_path,
)

BOARD_CLI = "agent-board"
MAX_FAILURES = 3
STOP_GRACE_SECONDS = 10.0
# паузы перед повтором после 1-й и 2-й подряд неудачи
RETRY_DELAYS = (5.0, 30.0)
LEFTOVER_MISSING_TEXT = "Старый процесс хода не найден (завершился или pid занят другим процессом), его не трогаем."
INTERRUPTED_TEXT = "Ход прерван перезапуском сервера, агент продолжит с сообщением о прерывании."
FAILURES_TEXT = ("Агент упал %d раза подряд, автоматический перезапуск остановлен: %s. "
                 "Напишите сообщение или смените статус, чтобы запустить его снова.")
PROJECT_SYSTEM_PROMPT = (
    "Ты агент проекта доски agent-board. Не пиши код и не меняй репозитории. "
    "Читай доску и общайся командой agent-board."
)
# причины, которые не добавляют ничего к стартовому промпту задачи
_START_ONLY = {"ready", "unblocked", "manual", "interrupted", "rebase"}

Key = Tuple[str, str]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_board_cli(bin_dir: Path) -> Path:
    """Обёртка agent-board для реальных харнессов: тот же интерпретатор и пакет, что у сервера."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "agent-board"
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    text = (
        "#!/bin/sh\n"
        "PYTHONPATH=%s\"${PYTHONPATH:+:$PYTHONPATH}\"\n"
        "export PYTHONPATH\n"
        "exec %s -m agent_board \"$@\"\n" % (shlex.quote(pkg_parent), shlex.quote(sys.executable))
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

    # --- жизненный цикл ---

    def start(self) -> None:
        self._server.hub.subscribe(self.on_event)
        self._inbox.put(("recover",))
        self._thread = threading.Thread(target=self._loop, name="board-runner", daemon=True)
        self._thread.start()

    def stop_leaving_agents_running(self) -> None:
        """Останавливает раннер; процессы идущих ходов продолжают работу, при следующем старте ходы считаются прерванными."""
        self._server.hub.unsubscribe(self.on_event)
        self._inbox.put(None)
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    stop = stop_leaving_agents_running

    def kill_all_for_tests(self, grace: float = 1.0) -> None:
        """Останавливает процессы всех идущих ходов (после stop, чтобы тесты не оставляли агентов)."""
        for turn in list(self._turns.values()):
            terminate(turn.proc, grace)

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
        elif kind == "recover":
            self._recover()
        elif kind == "sync":
            item[1].set()

    def _report_failure(self, item: tuple, error: Exception) -> None:
        if item[0] == "event":
            slug, key = item[1], item[2].data.get("key")
        elif item[0] in ("done", "retry"):
            slug, key = item[1]
        else:
            return
        try:
            board = self._board(slug)
            if key == PROJECT_THREAD or key in board.store.load_tasks():
                board.post_message(key, "system", "Ошибка раннера: %r" % error)
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
        config = dict(board.store.load_config().harness)
        if task is not None and task.harness:
            config["type"] = task.harness
        return make_harness(config)

    def _env(self, board: Board, key: str) -> dict:
        project = key == PROJECT_THREAD
        return {
            "AGENT_BOARD_HOME": str(board_home()),
            "AGENT_BOARD_PROJECT": board.store.slug,
            "AGENT_BOARD_ACTOR": "project_agent" if project else "agent",
            "AGENT_BOARD_TASK": "" if project else key,
            "PATH": str(board.store.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }

    @staticmethod
    def _kill_leftover(board: Board, thread: str, record) -> None:
        # процесс прерванного хода мог пережить сервер: две копии на одной сессии недопустимы
        if not isinstance(record, dict):
            return
        if is_same_process(record):
            terminate_group(record["pgid"], STOP_GRACE_SECONDS)
            return
        print("runner: старый процесс хода %s не найден: %r" % (thread, record), file=sys.stderr)
        board.post_message(thread, "system", LEFTOVER_MISSING_TEXT)

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
            for key, task in sorted(tasks.items()):
                if task.agent_state == "running":
                    self._kill_leftover(board, key, task.turn_process)
                    board.update_task(key, "system", {"agent_state": "idle", "turn_process": None})
                    board.post_message(key, "system", INTERRUPTED_TEXT)
                    board.record("agent_wake", {"key": key, "reason": "interrupted"})
                    woken.add(key)
                elif task.agent_state == "waiting_turn":
                    board.update_task(key, "system", {"agent_state": "idle"})
                    reason = "interrupted" if task.session_id else "ready"
                    board.record("agent_wake", {"key": key, "reason": reason})
                    woken.add(key)
            if meta.get("project_agent_state") == "running":
                self._kill_leftover(board, PROJECT_THREAD, meta.get("project_turn_process"))
                board.update_meta({"project_agent_state": "idle", "project_turn_process": None})
                board.post_message(PROJECT_THREAD, "system", INTERRUPTED_TEXT)
                board.record("agent_wake", {"key": PROJECT_THREAD, "reason": "interrupted"})
                woken.add(PROJECT_THREAD)
            elif config.stage == "work" and not config.project_session_id:
                self._wake(slug, PROJECT_THREAD, {"key": PROJECT_THREAD, "reason": "ready"})
                woken.add(PROJECT_THREAD)
            for key in restored:
                state = meta.get("project_agent_state") if key == PROJECT_THREAD else tasks[key].agent_state
                k = (slug, key)
                # ошибочные ждут действия пользователя, как и до перезапуска
                if key not in woken and state != "error" and k not in self._turns and k not in self._waiting:
                    self._schedule(k)

    # --- реакции на события ---

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
                if d.get("to") == Status.APPROVED.value:
                    # коммитится только законченная работа: идущий ход останавливаем до коммита
                    self._interrupt_turn(k)
                    self._commit(slug, k[1])
                if self._pending.get(k) and k not in self._turns and k not in self._waiting:
                    self._schedule(k)
        elif event.type == "agent_stop":
            self._stop(slug, str(d.get("key")))
        elif event.type == "agent_wake":
            board = self._board(slug)
            reason = self._ctx.enrich(slug, board.store, event)
            key = str(d.get("key"))
            if reason.get("reason") == "answer" or (reason.get("reason") == "message" and reason.get("author") == "user"):
                self._reset_failures((slug, key))
            self._wake(slug, key, reason)

    def _own_worktree(self, config, task: Task) -> Path:
        try:
            repo, _ = self._repo(config, task)
        except _PrepareError as e:
            raise _ForeignWorktree("не удалось проверить worktree %s: %s" % (task.worktree, e))
        expected = worktree_path(repo, task.key)
        if not task.worktree or Path(task.worktree).resolve() != expected.resolve():
            raise _ForeignWorktree("worktree %s не совпадает с ожидаемым %s" % (task.worktree, expected))
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
            board.post_message(key, "system", "Не удалось закоммитить одобренную работу, задача возвращена на ревью: %s" % e)
            return
        if sha:
            board.record("task_committed", {"key": key, "sha": sha})

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

    # --- очередь, лимиты, повторы ---

    def _schedule(self, k: Key) -> None:
        if self._failures.get(k, 0) >= MAX_FAILURES:
            return
        if self._has_capacity(k):
            self._run(k)
        else:
            self._waiting.append(k)
            self._set_state(k, "waiting_turn")

    def _has_capacity(self, k: Key) -> bool:
        config = self._board(k[0]).store.load_config()
        if len(self._turns) >= config.global_max_agents:
            return False
        # агент проекта учитывается только в общем лимите, чтобы эскалации не ждали задач проекта
        if k[1] == PROJECT_THREAD:
            return True
        in_project = sum(1 for s, key in self._turns if s == k[0] and key != PROJECT_THREAD)
        return in_project < config.max_agents

    def _drain(self) -> None:
        for k in list(self._waiting):
            if k in self._waiting and self._has_capacity(k):
                self._waiting.remove(k)
                self._run(k)

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

    def _set_state(self, k: Key, state: str, error: Optional[str] = None) -> None:
        board = self._board(k[0])
        if k[1] == PROJECT_THREAD:
            fields = {"project_agent_state": state}
            if error is not None:
                fields["project_turn_error"] = error
            board.update_meta(fields)
            return
        if k[1] not in board.store.load_tasks():
            return
        fields = {"agent_state": state}
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
            message = str(e) if isinstance(e, _PrepareError) else "ошибка раннера: %r" % e
            try:
                self._set_state(k, "error", message)
                self._board(k[0]).post_message(k[1], "system", "Агент не запущен: %s" % message)
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
        if task.session_id:
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
            raise _PrepareError("у задачи есть сессия, но нет worktree")
        else:
            self._start_task(k, board, task, tasks, config, deliver)

    def _repo(self, config, task: Task) -> Tuple[Path, str]:
        repos = [r for r in config.repos if isinstance(r, dict) and r.get("path")]
        if task.repo:
            repo = next((r for r in repos if r.get("name") == task.repo), None)
            if repo is None:
                raise _PrepareError("репозиторий %s не найден в настройках проекта" % task.repo)
        elif len(repos) == 1:
            repo = repos[0]
        else:
            raise _PrepareError("у задачи не указан репозиторий, а в проекте их %d" % len(repos))
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
                raise _PrepareError("не удалось подготовить worktree: %s" % e)
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
            raise _PrepareError(str(e))
        reasons = deliver
        rebases = [r for r in deliver if r.get("reason") == "rebase"]
        if rebases:
            # несколько ребейзов подряд сводятся к одному — на актуальную базу
            reasons = [r for r in deliver if r.get("reason") != "rebase"] + [self._rebase(board, task, tasks, config, rebases[-1])]
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
            ok, files = rebase_carrying_changes(self._own_worktree(config, task), base)
        except GitError:
            ok, files = False, []
        reason.update(base=base, rebase_ok=ok, conflict_files=files)
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
                result = TurnResult(session_id=turn.session_id, ok=False, error="ошибка раннера: %r" % e, exit_code=-1)
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
            board.update_meta({"project_agent_state": "running", "project_turn_error": "", "project_turn_process": record})
        else:
            board.update_task(k[1], "system", {"agent_state": "running", "turn_error": "", "turn_process": record})
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

    def _record_result(self, k: Key, turn: _Turn, result: TurnResult) -> None:
        board = self._board(k[0])
        project = k[1] == PROJECT_THREAD
        if turn.stopped:
            state, error, event = "idle", "", ("turn_finished", {"key": k[1], "stopped": True})
            session_id = result.session_id or turn.session_id
        elif result.ok:
            self._failures.pop(k, None)
            state, error, event = "idle", "", ("turn_finished", {"key": k[1]})
            session_id = result.session_id or turn.session_id
        else:
            self._failures[k] = self._failures.get(k, 0) + 1
            error = result.error or "ход завершился с ошибкой"
            state, event = "error", ("turn_failed", {"key": k[1], "error": error})
            # неудачный первый ход начнётся заново новой сессией, причины вернутся в следующий ход
            session_id = None if turn.fresh else (result.session_id or turn.session_id)
            self._pending[k] = turn.reasons + self._pending.get(k, [])
        if project:
            board.update_config({"project_session_id": session_id})
            board.update_meta({"project_agent_state": state, "project_turn_error": error, "project_turn_process": None})
        elif k[1] in board.store.load_tasks():
            self._update_task_after_turn(board, k[1], state, error, session_id)
        board.record(event[0], event[1])
        if state == "error" and self._failures.get(k, 0) >= MAX_FAILURES:
            board.post_message(k[1], "system", FAILURES_TEXT % (MAX_FAILURES, error))

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
