from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

WORKTREES_DIR = ".agent-board-worktrees"
TIMEOUT = 120
CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ")


class GitError(RuntimeError):
    pass


def _git(cwd: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(cwd), capture_output=True, text=True, env=env, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitError("git {} timed out after {}s".format(" ".join(args), TIMEOUT))
    except OSError as e:
        raise GitError("git {} could not run in {}: {}".format(" ".join(args), cwd, e))


def run_git(repo: Path, *args: str) -> str:
    res = _git(repo, *args)
    if res.returncode != 0:
        out = (res.stdout + res.stderr).strip()
        raise GitError("git {} failed: {}".format(" ".join(args), out))
    return res.stdout


def worktree_path(repo: Path, key: str) -> Path:
    return Path(repo) / WORKTREES_DIR / key


def _ensure_excluded(repo: Path) -> None:
    rel = run_git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    exclude = (Path(repo) / rel).resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = WORKTREES_DIR + "/"
    existing = exclude.read_text() if exclude.exists() else ""
    if line in existing.splitlines():
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a") as f:
        f.write(prefix + line + "\n")


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch).returncode == 0


def _check_no_merge_state(path: Path) -> None:
    if _git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        raise GitError("merge in progress in {}: resolve or abort it first".format(path))
    if run_git(path, "diff", "--name-only", "--diff-filter=U").strip():
        raise GitError("unmerged paths in {}: resolve them first".format(path))


def ensure_worktree(repo: Path, key: str, branch: str, base: str, merge: List[str]) -> Path:
    repo = Path(repo)
    path = worktree_path(repo, key)
    if (path / ".git").exists():
        _check_no_merge_state(path)
    else:
        _ensure_excluded(repo)
        run_git(repo, "worktree", "prune")
        if _branch_exists(repo, branch):
            run_git(repo, "worktree", "add", str(path), branch)
        else:
            run_git(repo, "worktree", "add", "-b", branch, str(path), base)
    for other in merge:
        run_git(path, "merge", "--no-edit", other)
    return path


def _merge_in_progress(path: Path) -> bool:
    return _git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def unmerged_files(path: Path) -> List[str]:
    out = run_git(path, "diff", "--name-only", "--diff-filter=U", "-z")
    return sorted({p for p in out.split("\0") if p})


def _files_with_markers(path: Path, names: List[str]) -> List[str]:
    found = []
    for name in names:
        try:
            lines = (Path(path) / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(line.startswith(CONFLICT_MARKERS) for line in lines):
            found.append(name)
    return found


def _check_no_markers(path: Path) -> None:
    staged = [p for p in run_git(path, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").split("\0") if p]
    marked = _files_with_markers(path, staged)
    if marked:
        raise GitError("conflict markers left in {}: {}".format(path, ", ".join(marked)))


def commit_all(path: Path, message: str) -> Optional[str]:
    merging = _merge_in_progress(path)
    if not merging:
        _check_no_merge_state(path)
    run_git(path, "add", "-A")
    if merging:
        # вливание с конфликтом агент разрешает правкой файлов: завершаем его, если следов конфликта не осталось
        unmerged = unmerged_files(path)
        if unmerged:
            raise GitError("unmerged paths in {}: {}".format(path, ", ".join(unmerged)))
    elif not run_git(path, "status", "--porcelain").strip():
        return None
    _check_no_markers(path)
    res = _git(path, "commit", "-m", message)
    if res.returncode != 0:
        out = (res.stdout + res.stderr).strip()
        if "identity" in out.lower() or "user.name" in out or "user.email" in out:
            raise GitError(
                "git has no identity configured (set user.name and user.email): " + out
            )
        raise GitError("git commit failed: " + out)
    return run_git(path, "rev-parse", "--short", "HEAD").strip()


def _rebase_in_progress(path: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        rel = run_git(path, "rev-parse", "--git-path", name).strip()
        if (Path(path) / rel).exists():
            return True
    return False


def rebase_onto(path: Path, new_base: str) -> bool:
    res = _git(path, "rebase", new_base)
    if res.returncode == 0:
        return True
    if _rebase_in_progress(path):
        run_git(path, "rebase", "--abort")
        return False
    raise GitError("git rebase failed: " + (res.stdout + res.stderr).strip())


def rebase_carrying_changes(path: Path, new_base: str) -> Tuple[bool, List[str]]:
    """Ребейз ветки с переносом незакоммиченной работы.

    (False, []) — ребейз не удался, ветка и изменения остались как были;
    (True, files) — изменения перенесены, в files остались маркеры конфликта.
    """
    dirty = bool(run_git(path, "status", "--porcelain").strip())
    if dirty:
        run_git(path, "stash", "push", "-u", "-m", "agent-board: rebase onto " + new_base)
    try:
        ok = rebase_onto(path, new_base)
    except GitError:
        if dirty:
            run_git(path, "stash", "pop")
        raise
    if not dirty:
        return ok, []
    res = _git(path, "stash", "pop")
    if res.returncode == 0:
        return ok, []
    conflicts = unmerged_files(path)
    if not conflicts:
        raise GitError("git stash pop failed: " + (res.stdout + res.stderr).strip())
    # индекс без конфликтных записей: агент разрешает конфликт правкой файлов, коммитит раннер
    run_git(path, "reset", "-q")
    # при конфликте git оставляет запись в stash; изменения уже в файлах, запись лишняя
    run_git(path, "stash", "drop", "-q")
    return ok, conflicts


def _untracked(path: Path) -> List[str]:
    out = run_git(path, "ls-files", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p]


def changed_files(path: Path, base: str) -> List[str]:
    tracked = run_git(path, "diff", "--name-only", "--no-renames", "-z", base, "--").split("\0")
    return sorted({p for p in tracked + _untracked(path) if p})


def diff(path: Path, base: str) -> str:
    parts = [run_git(path, "diff", "--no-ext-diff", "--no-color", "--no-textconv", "--no-renames", base, "--")]
    for name in _untracked(path):
        # --no-index завершается кодом 1, когда есть различия, это не ошибка
        res = _git(path, "diff", "--no-index", "--no-ext-diff", "--no-color", "--no-textconv", "--", os.devnull, name)
        if res.returncode not in (0, 1):
            raise GitError("git diff --no-index failed: " + res.stderr.strip())
        parts.append(res.stdout)
    return "".join(parts)
