from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

from .board import Board
from .model import Status
from .store import atomic_write_json


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Entry:
    def __init__(self, task: str, pid: int):
        self.task = task
        self.pid = pid
        self.position: Optional[int] = None
        self.acquired = False
        # статус, в который вернуть задачу, пока она стояла в очереди
        self.restore: Optional[Status] = None


class ResourceQueue:
    """Эксклюзивная FIFO-очередь на объявленные ресурсы проекта; держатель — первая запись."""

    def __init__(self, board: Board):
        self._board = board
        self._lock = threading.RLock()
        self._queues: Dict[str, List[_Entry]] = {}
        self._path = board.store.root / "locks.json"
        self._load()
        self.reap()

    def acquire(self, resource: str, task: str, pid: int) -> int:
        with self._lock:
            self._check(resource, task)
            self.reap()
            queue = self._queues.setdefault(resource, [])
            if not any(e.task == task and e.pid == pid for e in queue):
                queue.append(_Entry(task, pid))
            self._refresh(resource)
            self._save()
            return self._find(resource, task, pid).position or 0

    def poll(self, resource: str, task: str, pid: int) -> int:
        # запись могла пропасть при рестарте сервера — тогда встаём в очередь заново
        return self.acquire(resource, task, pid)

    def release(self, resource: str, task: str, pid: int) -> None:
        with self._lock:
            self._check(resource, task)
            queue = self._queues.get(resource, [])
            entry = next((e for e in queue if e.task == task and e.pid == pid), None)
            if entry is not None:
                queue.remove(entry)
                self._leave(resource, entry)
            self.reap()
            self._refresh(resource)
            self._save()

    def reap(self) -> None:
        """Снимает держателей и ожидающих, чьи процессы умерли."""
        with self._lock:
            for resource, queue in self._queues.items():
                dead = [e for e in queue if not _pid_alive(e.pid)]
                for entry in dead:
                    queue.remove(entry)
                    self._leave(resource, entry)
                if dead:
                    self._refresh(resource)
                    self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for resource, items in raw.items():
            queue = []
            for index, item in enumerate(items):
                entry = _Entry(item["task"], int(item["pid"]))
                entry.acquired = bool(item.get("acquired"))
                entry.restore = Status(item["restore"]) if item.get("restore") else None
                entry.position = index
                queue.append(entry)
            self._queues[resource] = queue

    def _save(self) -> None:
        data = {
            resource: [
                {"task": e.task, "pid": e.pid, "acquired": e.acquired, "restore": e.restore.value if e.restore else None}
                for e in queue
            ]
            for resource, queue in self._queues.items()
        }
        atomic_write_json(self._path, data)

    def _check(self, resource: str, task: str) -> None:
        declared = [r.get("name") for r in self._board.store.load_config().resources]
        if resource not in declared:
            raise ValueError("Ресурс %r не объявлен в проекте. Объявленные: %s" % (resource, ", ".join(declared) or "нет"))
        if task not in self._board.store.load_tasks():
            raise KeyError("Неизвестная задача: %s" % task)

    def _find(self, resource: str, task: str, pid: int) -> _Entry:
        return next(e for e in self._queues[resource] if e.task == task and e.pid == pid)

    def _refresh(self, resource: str) -> None:
        """Пересчитывает позиции и публикует изменения: ожидание, получение ресурса."""
        for index, entry in enumerate(self._queues.get(resource, [])):
            if entry.position == index:
                continue
            entry.position = index
            if index == 0:
                entry.acquired = True
                self._restore_status(entry)
                self._board.record("lock_acquired", {"key": entry.task, "resource": resource})
            else:
                self._mark_queued(entry)
                self._board.record("lock_waiting", {"key": entry.task, "resource": resource, "position": index})

    def _leave(self, resource: str, entry: _Entry) -> None:
        self._restore_status(entry)
        if entry.acquired:
            self._board.record("lock_released", {"key": entry.task, "resource": resource})

    def _mark_queued(self, entry: _Entry) -> None:
        task = self._board.store.load_tasks().get(entry.task)
        if task is None or task.status == Status.QUEUED:
            return
        prev = task.status
        if self._board.transition_if(entry.task, prev, Status.QUEUED, "system"):
            entry.restore = prev

    def _restore_status(self, entry: _Entry) -> None:
        """Возвращает прежний статус, когда задача больше не ждёт ни один ресурс проекта."""
        siblings = [e for q in self._queues.values() for e in q if e.task == entry.task and e is not entry]
        remembered = entry.restore
        entry.restore = None
        if any((e.position or 0) > 0 for e in siblings):
            for e in siblings:
                if remembered is not None and e.restore is None:
                    e.restore = remembered
                    break
            return
        for e in siblings:
            remembered = remembered or e.restore
            e.restore = None
        if remembered is not None:
            self._board.transition_if(entry.task, Status.QUEUED, remembered, "system")
