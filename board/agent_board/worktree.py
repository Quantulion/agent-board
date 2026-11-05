from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
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


def _check_no_rebase(path: Path) -> None:
    # чужой (начатый агентом или человеком) ребейз не трогаем и не прерываем
    if _rebase_in_progress(path):
        raise GitError("rebase already in progress in {}: finish or abort it first".format(path))


def rebase_onto(path: Path, new_base: str) -> bool:
    _check_no_rebase(path)
    res = _git(path, "rebase", new_base)
    if res.returncode == 0:
        return True
    if _rebase_in_progress(path):
        run_git(path, "rebase", "--abort")
        return False
    raise GitError("git rebase failed: " + (res.stdout + res.stderr).strip())


def carry_ref(key: str) -> str:
    return "refs/agent-board/%s/carry" % key


@dataclass
class RebaseOutcome:
    """ok — ветка на новой базе; conflict_files — файлы с маркерами после возврата работы;
    carried_ref — работу не удалось вернуть в файлы, она сохранена в этом ref."""

    ok: bool
    conflict_files: List[str] = field(default_factory=list)
    carried_ref: Optional[str] = None


def _git_with_index(path: Path, index: str, *args: str, stdin: str = "") -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_INDEX_FILE=index)
    try:
        res = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(path), input=stdin,
                             capture_output=True, text=True, env=env, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GitError("git {} could not run in {}: {}".format(" ".join(args), path, e))
    if res.returncode != 0:
        raise GitError("git {} failed: {}".format(" ".join(args), (res.stdout + res.stderr).strip()))
    return res.stdout


def _snapshot(path: Path, message: str) -> str:
    """Коммит в формате git stash (рабочие файлы, индекс, неотслеживаемые) без записи в общий список stash."""
    head = run_git(path, "rev-parse", "HEAD").strip()
    index_tree = run_git(path, "write-tree").strip()
    index_commit = run_git(path, "commit-tree", index_tree, "-p", head, "-m", "index on " + message).strip()
    parents = ["-p", head, "-p", index_commit]
    with tempfile.TemporaryDirectory(prefix="agent-board-carry-") as tmp:
        work_index = os.path.join(tmp, "work")
        _git_with_index(path, work_index, "read-tree", index_tree)
        _git_with_index(path, work_index, "add", "-u")
        work_tree = _git_with_index(path, work_index, "write-tree").strip()
        untracked = _untracked(path)
        if untracked:
            new_index = os.path.join(tmp, "untracked")
            _git_with_index(path, new_index, "add", "--pathspec-from-file=-", "--pathspec-file-nul",
                            stdin="\0".join(untracked))
            untracked_tree = _git_with_index(path, new_index, "write-tree").strip()
            untracked_commit = run_git(path, "commit-tree", untracked_tree, "-m", "untracked files on " + message).strip()
            parents += ["-p", untracked_commit]
    return run_git(path, "commit-tree", work_tree, *parents, "-m", message).strip()


def _restore(path: Path, snapshot: str) -> Tuple[bool, List[str]]:
    """Возвращает сохранённую работу в файлы: (вернулась ли, файлы с маркерами конфликта).

    Если не вернулась, файлы остаются как в HEAD."""
    res = _git(path, "stash", "apply", snapshot)
    if res.returncode == 0:
        return True, []
    conflicts = unmerged_files(path)
    if not conflicts:
        # работа могла вернуться частично: дерево возвращаем к HEAD, вся работа остаётся в снимке
        run_git(path, "reset", "-q", "--hard")
        run_git(path, "clean", "-fdq")
        return False, []
    # индекс без конфликтных записей: агент разрешает конфликт правкой файлов, коммитит раннер
    run_git(path, "reset", "-q")
    return True, conflicts


def rebase_carrying_changes(path: Path, new_base: str, ref: str) -> RebaseOutcome:
    """Ребейз ветки с переносом незакоммиченной работы.

    Работа на время ребейза сохраняется в ref (общий список git stash не трогается) и после возврата в файлы
    удаляется. Если вернуть её не удалось, файлы остаются как в HEAD, а ref остаётся и указан в carried_ref.
    """
    _check_no_rebase(path)
    if _git(path, "rev-parse", "-q", "--verify", ref).returncode == 0:
        raise GitError("previous work carried at {} is not restored yet: restore and delete it first".format(ref))
    if not run_git(path, "status", "--porcelain").strip():
        return RebaseOutcome(rebase_onto(path, new_base))
    snapshot = _snapshot(path, "agent-board: rebase onto " + new_base)
    run_git(path, "update-ref", ref, snapshot)
    run_git(path, "reset", "-q", "--hard")
    run_git(path, "clean", "-fdq")
    try:
        ok = rebase_onto(path, new_base)
    except GitError as e:
        restored, _ = _restore(path, snapshot)
        if not restored:
            raise GitError("{}; uncommitted work is saved at {}".format(e, ref))
        run_git(path, "update-ref", "-d", ref)
        raise
    restored, conflicts = _restore(path, snapshot)
    if not restored:
        return RebaseOutcome(ok, [], carried_ref=ref)
    run_git(path, "update-ref", "-d", ref)
    return RebaseOutcome(ok, conflicts)


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
