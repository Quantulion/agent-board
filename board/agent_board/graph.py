from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .model import DEP_SATISFIED_CHAINS, DEP_SATISFIED_STRICT, Status, Task

_UNMERGED = (Status.APPROVED, Status.SENT)


def open_deps(task: Task, tasks: Dict[str, Task], chains: bool) -> List[str]:
    satisfied = DEP_SATISFIED_CHAINS if chains else DEP_SATISFIED_STRICT
    result = []
    for key in task.deps:
        dep = tasks.get(key)
        if dep is None:
            result.append(key)
        elif dep.status != Status.CANCELLED and dep.status not in satisfied:
            result.append(key)
    return result


def display_status(task: Task, tasks: Dict[str, Task], chains: bool) -> Status:
    if task.status == Status.NOT_STARTED and (task.external_blocker or open_deps(task, tasks, chains)):
        return Status.BLOCKED
    return task.status


def ready_tasks(tasks: Dict[str, Task], chains: bool) -> List[str]:
    return sorted(
        key
        for key, t in tasks.items()
        if t.status == Status.NOT_STARTED and not t.external_blocker and not open_deps(t, tasks, chains)
    )


def _find_cycle(deps_of: Dict[str, List[str]]) -> Optional[List[str]]:
    white, gray, black = 0, 1, 2
    color = {key: white for key in deps_of}
    path: List[str] = []

    def visit(key: str) -> Optional[List[str]]:
        color[key] = gray
        path.append(key)
        for dep in deps_of[key]:
            if dep not in color:
                continue
            if color[dep] == gray:
                return path[path.index(dep):] + [dep]
            if color[dep] == white:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        color[key] = black
        return None

    for key in sorted(deps_of):
        if color[key] == white:
            found = visit(key)
            if found:
                return found
    return None


def find_cycle(tasks: Dict[str, Task]) -> Optional[List[str]]:
    return _find_cycle({key: list(t.deps) for key, t in tasks.items()})


def validate_deps(key: str, deps: List[str], tasks: Dict[str, Task]) -> None:
    for dep in deps:
        if dep == key:
            raise ValueError("Задача не может зависеть от себя: %s" % key)
        if dep not in tasks:
            raise ValueError("Зависимость от неизвестной задачи: %s" % dep)
    graph = {k: list(t.deps) for k, t in tasks.items()}
    graph[key] = list(deps)
    cycle = _find_cycle(graph)
    if cycle:
        raise ValueError("Зависимости образуют цикл: %s" % " → ".join(cycle))


def base_for(task: Task, tasks: Dict[str, Task], chains: bool, default_base: str) -> Tuple[str, List[str]]:
    if not chains:
        return default_base, []
    branches = [
        tasks[k].branch
        for k in task.deps
        if k in tasks and tasks[k].status in _UNMERGED and tasks[k].branch
    ]
    if not branches:
        return default_base, []
    if len(branches) == 1:
        return branches[0], []
    return default_base, branches


def critical_path(tasks: Dict[str, Task]) -> List[str]:
    active = {k: t for k, t in tasks.items() if t.status != Status.CANCELLED}
    cycle = _find_cycle({k: [d for d in t.deps if d in active] for k, t in active.items()})
    if cycle:
        raise ValueError("Зависимости образуют цикл: %s" % " → ".join(cycle))

    best: Dict[str, Tuple[float, List[str]]] = {}

    def solve(key: str) -> Tuple[float, List[str]]:
        if key in best:
            return best[key]
        t = active[key]
        weight = 1.0 if t.estimate is None else float(t.estimate)
        prev: Tuple[float, List[str]] = (0.0, [])
        for dep in sorted(d for d in t.deps if d in active):
            cand = solve(dep)
            if cand[0] > prev[0]:
                prev = cand
        best[key] = (prev[0] + weight, prev[1] + [key])
        return best[key]

    result: Tuple[float, List[str]] = (0.0, [])
    for key in sorted(active):
        cand = solve(key)
        if cand[0] > result[0]:
            result = cand
    return result[1]


def dependents(key: str, tasks: Dict[str, Task]) -> List[str]:
    return sorted(k for k, t in tasks.items() if key in t.deps)
