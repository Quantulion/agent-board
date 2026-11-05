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
from .harness import Harness, TurnResult, make_harness, terminate
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
        self._waiting: Deque[Key] = deque()
        self._failures: Dict[Key, int] = {}

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
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def _dispatch(self, item: tuple) -> None:
        kind = item[0]
        if kind == "event":
            self._on_board_event(item[1], item[2])
        elif kind == "done":
            self._on_turn_done(item[1], item[2], item[3])
        elif kind == "recover":
            self._recover()
        elif kind == "sync":
            item[1].set()

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

    def _recover(self) -> None:
        for slug in list_projects():
            try:
                board = self._board(slug)
            except (KeyError, ValueError):
                continue
            for key, task in sorted(board.store.load_tasks().items()):
                if task.agent_state == "running":
                    board.update_task(key, "system", {"agent_state": "idle"})
                    board.post_message(key, "system", INTERRUPTED_TEXT)
                    board.record("agent_wake", {"key": key, "reason": "interrupted"})
                elif task.agent_state == "waiting_turn":
                    # очередь раннера живёт в памяти: возвращаем задачу в неё
                    board.update_task(key, "system", {"agent_state": "idle"})
                    reason = "interrupted" if task.session_id else "ready"
                    board.record("agent_wake", {"key": key, "reason": reason})
            meta = board.store.load_meta()
            config = board.store.load_config()
            if meta.get("project_agent_state") == "running":
                board.update_meta({"project_agent_state": "idle"})
                board.post_message(PROJECT_THREAD, "system", INTERRUPTED_TEXT)
                board.record("agent_wake", {"key": PROJECT_THREAD, "reason": "interrupted"})
            elif config.stage == "work" and not config.project_session_id:
                self._wake(slug, PROJECT_THREAD, {"key": PROJECT_THREAD, "reason": "ready"})

    # --- реакции на события ---

    def _on_board_event(self, slug: str, event: Event) -> None:
        self._ctx.observe(slug, event)
        d = event.data
        if event.type == "status_changed":
            k = (slug, str(d.get("key")))
            if d.get("actor") == "user":
                self._failures.pop(k, None)
                if d.get("to") == Status.APPROVED.value:
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
                self._failures.pop((slug, key), None)
            self._wake(slug, key, reason)

    def _commit(self, slug: str, key: str) -> None:
        board = self._board(slug)
        task = board.store.load_tasks().get(key)
        if task is None or not task.worktree:
            return
        try:
            sha = commit_all(Path(task.worktree), "%s %s" % (key, task.title))
        except GitError as e:
            board.post_message(key, "system", "Не удалось закоммитить одобренную работу: %s" % e)
            return
        if sha:
            board.record("task_committed", {"key": key, "sha": sha})

    def _stop(self, slug: str, key: str) -> None:
        k = (slug, key)
        self._pending.pop(k, None)
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
        self._pending.setdefault(k, []).append(reason)
        if k in self._turns or k in self._waiting:
            return
        self._schedule(k)

    # --- очередь и лимиты ---

    def _schedule(self, k: Key) -> None:
        if self._failures.get(k, 0) >= MAX_FAILURES:
            return
        if k[1] == PROJECT_THREAD or self._has_capacity(k[0]):
            self._run(k)
        else:
            self._waiting.append(k)
            self._set_state(k, "waiting_turn")

    def _has_capacity(self, slug: str) -> bool:
        config = self._board(slug).store.load_config()
        task_turns = [k for k in self._turns if k[1] != PROJECT_THREAD]
        in_project = sum(1 for k in task_turns if k[0] == slug)
        return in_project < config.max_agents and len(task_turns) < config.global_max_agents

    def _drain(self) -> None:
        for k in list(self._waiting):
            if k in self._waiting and self._has_capacity(k[0]):
                self._waiting.remove(k)
                self._run(k)

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
        self._pending[k] = [r for r in reasons if not any(r is x for x in deliver)]
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

    def _start_task(self, k: Key, board: Board, task: Task, tasks, config, reasons: List[dict]) -> None:
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
                self._pending[k] = reasons + self._pending.get(k, [])
                raise _PrepareError("не удалось подготовить worktree: %s" % e)
        harness = self._harness(board)
        session_id = harness.new_session_id()
        board.update_task(key, "system", {
            "worktree": str(path), "branch": branch, "base_branch": base,
            "harness": config.harness.get("type", "claude"), "session_id": session_id,
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
        extra = [r for r in reasons if r.get("reason") not in _START_ONLY]
        if extra:
            text += "\n" + prompts.wake_message(extra, "", BOARD_CLI)
        proc = harness.start_turn(path, text, session_id, self._log(board, key), self._env(board, key))
        self._begin(k, board, harness, proc, _Turn(proc, reasons, True, session_id))

    def _resume_task(self, k: Key, board: Board, task: Task, tasks, config, reasons: List[dict]) -> None:
        path = Path(task.worktree)
        rebases = [r for r in reasons if r.get("reason") == "rebase"]
        if rebases:
            # несколько ребейзов подряд сводятся к одному — на актуальную базу
            reasons = [r for r in reasons if r.get("reason") != "rebase"] + [self._rebase(board, task, tasks, config, rebases[-1])]
        harness = self._harness(board, task)
        message = prompts.wake_message(reasons, "", BOARD_CLI)
        proc = harness.resume_turn(path, task.session_id, message, self._log(board, task.key), self._env(board, task.key))
        self._begin(k, board, harness, proc, _Turn(proc, reasons, False, task.session_id))

    def _rebase(self, board: Board, task: Task, tasks, config, reason: dict) -> dict:
        reason = dict(reason)
        try:
            _, default_base = self._repo(config, task)
        except _PrepareError:
            default_base = task.base_branch or "main"
        base, merges = base_for(task, tasks, config.chains, default_base)
        if merges:
            reason.update(base=", ".join(merges), rebase_ok=False)
            return reason
        try:
            ok, files = rebase_carrying_changes(Path(task.worktree), base)
        except GitError:
            ok, files = False, []
        reason.update(base=base, rebase_ok=ok, conflict_files=files)
        if ok and task.base_branch != base:
            board.update_task(task.key, "system", {"base_branch": base})
        return reason

    def _run_project(self, k: Key) -> None:
        board = self._board(k[0])
        config = board.store.load_config()
        reasons = self._pending.pop(k, [])
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
            board.update_config({"project_session_id": session_id})
            text = prompts.project_prompt(config, board.snapshot(), BOARD_CLI)
            extra = [r for r in reasons if r.get("reason") not in _START_ONLY]
            if extra:
                text += "\n" + prompts.wake_message(extra, "", BOARD_CLI)
            proc = harness.start_turn(cwd, text, session_id, log, env)
            turn = _Turn(proc, reasons, True, session_id)
        board.update_meta({"project_last_seen": last_seq})
        self._begin(k, board, harness, proc, turn)

    @staticmethod
    def _log(board: Board, key: str) -> Path:
        return board.store.root / "logs" / (key + ".jsonl")

    def _begin(self, k: Key, board: Board, harness: Harness, proc, turn: _Turn) -> None:
        self._turns[k] = turn
        self._set_state(k, "running", "")
        board.record("turn_started", {"key": k[1]})
        log = self._log(board, k[1])

        def wait() -> None:
            try:
                result = harness.finish(proc, log)
            except Exception as e:
                result = TurnResult(session_id=turn.session_id, ok=False, error="ошибка раннера: %r" % e, exit_code=-1)
            self._inbox.put(("done", k, turn, result))

        threading.Thread(target=wait, name="board-turn-%s" % k[1], daemon=True).start()

    def _on_turn_done(self, k: Key, turn: _Turn, result: TurnResult) -> None:
        if self._turns.get(k) is not turn:
            return
        del self._turns[k]
        try:
            self._record_result(k, turn, result)
        finally:
            if self._pending.get(k) and k not in self._waiting:
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
            failures = self._failures.get(k, 0) + 1
            self._failures[k] = failures
            error = result.error or "ход завершился с ошибкой"
            state, event = "error", ("turn_failed", {"key": k[1], "error": error})
            # неудачный первый ход начнётся заново новой сессией, остальные причины вернутся в следующий ход
            session_id = None if turn.fresh else (result.session_id or turn.session_id)
            self._pending[k] = turn.reasons + self._pending.get(k, [])
        if project:
            board.update_config({"project_session_id": session_id})
            board.update_meta({"project_agent_state": state, "project_turn_error": error})
        elif k[1] in board.store.load_tasks():
            self._update_task_after_turn(board, k[1], state, error, session_id)
        board.record(event[0], event[1])
        if state == "error" and self._failures.get(k, 0) >= MAX_FAILURES:
            board.post_message(k[1], "system", FAILURES_TEXT % (MAX_FAILURES, error))

    def _update_task_after_turn(self, board: Board, key: str, state: str, error: str, session_id: Optional[str]) -> None:
        task = board.store.load_tasks()[key]
        fields = {"agent_state": state, "turn_error": error, "last_turn_at": self._clock(), "session_id": session_id}
        if task.worktree and task.base_branch:
            try:
                fields["changed_files"] = changed_files(Path(task.worktree), task.base_branch)
            except GitError:
                pass
        board.update_task(key, "system", fields)
