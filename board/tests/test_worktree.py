from __future__ import annotations

import subprocess

import pytest

from agent_board.worktree import (
    GitError,
    changed_files,
    commit_all,
    diff,
    ensure_worktree,
    RebaseOutcome,
    carry_ref,
    rebase_carrying_changes,
    rebase_onto,
    run_git,
    worktree_path,
)


def commit_file(repo, name, text, msg=None):
    (repo / name).write_text(text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", msg or name)


def make_branch(repo, branch, name, text):
    run_git(repo, "checkout", "-b", branch, "main")
    commit_file(repo, name, text)
    run_git(repo, "checkout", "main")


def test_ensure_worktree_creates_from_base(git_repo):
    # arrange / act
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    # assert
    assert p == worktree_path(git_repo, "T-1") == git_repo / ".agent-board-worktrees" / "T-1"
    assert (p / "a.txt").read_text() == "base\n"
    assert run_git(p, "branch", "--show-current").strip() == "T-1"


def test_ensure_worktree_is_idempotent(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "wip.txt").write_text("x")
    # act
    p2 = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    # assert
    assert p2 == p and (p / "wip.txt").exists()


def test_exclude_written_once(git_repo):
    # arrange / act
    ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    ensure_worktree(git_repo, "T-2", "T-2", "main", [])
    # assert
    lines = (git_repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count(".agent-board-worktrees/") == 1
    assert run_git(git_repo, "status", "--porcelain").strip() == ""


def test_ensure_worktree_merges_branches(git_repo):
    # arrange
    make_branch(git_repo, "dep-1", "one.txt", "1")
    make_branch(git_repo, "dep-2", "two.txt", "2")
    # act
    p = ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])
    # assert
    assert (p / "one.txt").exists() and (p / "two.txt").exists()


def test_ensure_worktree_merge_conflict_raises_and_keeps_worktree(git_repo):
    # arrange
    make_branch(git_repo, "dep-1", "a.txt", "one\n")
    make_branch(git_repo, "dep-2", "a.txt", "two\n")
    # act / assert
    with pytest.raises(GitError):
        ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])
    assert worktree_path(git_repo, "T-3").exists()


def test_ensure_worktree_recreates_after_dir_deleted(git_repo):
    # arrange
    import shutil

    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "f.txt").write_text("f")
    commit_all(p, "T-1 work")
    shutil.rmtree(p)
    # act
    p2 = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    # assert
    assert (p2 / "f.txt").read_text() == "f"


def test_commit_all_commits_and_returns_none_when_clean(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "new.txt").write_text("n")
    # act
    sha = commit_all(p, "T-1 title")
    again = commit_all(p, "T-1 title")
    # assert
    assert sha and run_git(p, "rev-parse", "--short", "HEAD").strip() == sha
    assert run_git(p, "log", "-1", "--format=%s").strip() == "T-1 title"
    assert again is None


def test_commit_all_without_identity_raises_git_error(git_repo, tmp_path, monkeypatch):
    # arrange
    run_git(git_repo, "config", "--unset", "user.name")
    run_git(git_repo, "config", "--unset", "user.email")
    run_git(git_repo, "config", "user.useConfigOnly", "true")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (git_repo / "x.txt").write_text("x")
    # act / assert
    with pytest.raises(GitError, match="identity"):
        commit_all(git_repo, "msg")


def test_rebase_onto_success_and_noop(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "mine.txt").write_text("m")
    commit_all(p, "mine")
    commit_file(git_repo, "main.txt", "x")
    # act
    ok = rebase_onto(p, "main")
    again = rebase_onto(p, "main")
    # assert
    assert ok and again
    assert (p / "main.txt").exists() and (p / "mine.txt").exists()


def test_rebase_onto_conflict_restores_tree(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    commit_all(p, "mine")
    head = run_git(p, "rev-parse", "HEAD").strip()
    commit_file(git_repo, "a.txt", "theirs\n")
    # act
    ok = rebase_onto(p, "main")
    # assert
    assert ok is False
    assert run_git(p, "rev-parse", "HEAD").strip() == head
    assert (p / "a.txt").read_text() == "mine\n"
    assert run_git(p, "status", "--porcelain").strip() == ""


def test_changed_files_sees_modified_new_and_untracked(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("changed\n")
    (p / "added.txt").write_text("1")
    run_git(p, "add", "added.txt")
    (p / "sub").mkdir()
    (p / "sub" / "untracked.txt").write_text("2")
    commit_file(p, "committed.txt", "3")
    (p / "a.txt").write_text("changed again\n")
    # act
    files = changed_files(p, "main")
    # assert
    assert files == ["a.txt", "added.txt", "committed.txt", "sub/untracked.txt"]


def test_diff_includes_untracked_and_keeps_index(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("changed\n")
    (p / "new.txt").write_text("hello\n")
    # act
    text = diff(p, "main")
    # assert
    assert "-base" in text and "+changed" in text
    assert "+++ b/new.txt" in text and "+hello" in text
    assert run_git(p, "diff", "--cached", "--name-only").strip() == ""


def test_ensure_worktree_reentry_after_conflict_raises(git_repo):
    # arrange
    make_branch(git_repo, "dep-1", "a.txt", "one\n")
    make_branch(git_repo, "dep-2", "a.txt", "two\n")
    with pytest.raises(GitError):
        ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])
    # act / assert
    with pytest.raises(GitError):
        ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])


def test_ensure_worktree_reentry_applies_missing_merges(git_repo):
    # arrange
    make_branch(git_repo, "dep-1", "one.txt", "1")
    p = ensure_worktree(git_repo, "T-3", "T-3", "main", [])
    # act
    ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1"])
    # assert
    assert (p / "one.txt").exists()


def test_commit_all_refuses_during_unresolved_merge(git_repo):
    # arrange
    make_branch(git_repo, "dep-1", "a.txt", "one\n")
    make_branch(git_repo, "dep-2", "a.txt", "two\n")
    with pytest.raises(GitError):
        ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])
    p = worktree_path(git_repo, "T-3")
    # act / assert
    with pytest.raises(GitError):
        commit_all(p, "T-3 title")


def test_rebase_onto_bad_ref_raises(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    # act / assert
    with pytest.raises(GitError):
        rebase_onto(p, "no-such-ref")


def test_rebase_onto_missing_worktree_raises(git_repo):
    # act / assert
    with pytest.raises(GitError):
        rebase_onto(git_repo / "nope", "main")


def test_changed_files_lists_both_paths_of_rename(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    run_git(p, "mv", "a.txt", "b.txt")
    # act
    files = changed_files(p, "main")
    # assert
    assert files == ["a.txt", "b.txt"]
    assert "rename" not in diff(p, "main")


def test_file_named_like_base_branch(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    commit_file(p, "main", "x")
    # act
    files = changed_files(p, "main")
    text = diff(p, "main")
    # assert
    assert files == ["main"]
    assert "+++ b/main" in text


def test_space_and_cyrillic_names(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "мой файл.txt").write_text("привет\n")
    # act
    files = changed_files(p, "main")
    text = diff(p, "main")
    # assert
    assert files == ["мой файл.txt"]
    assert "мой файл.txt" in text


def test_run_git_timeout_raises_git_error(git_repo, monkeypatch):
    # arrange
    import agent_board.worktree as wt

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(wt.subprocess, "run", boom)
    # act / assert
    with pytest.raises(GitError, match="timed out"):
        run_git(git_repo, "status")


# --- завершение вливания при коммите (P6) ---

def conflicted_worktree(git_repo):
    make_branch(git_repo, "dep-1", "a.txt", "one\n")
    make_branch(git_repo, "dep-2", "a.txt", "two\n")
    with pytest.raises(GitError):
        ensure_worktree(git_repo, "T-3", "T-3", "main", ["dep-1", "dep-2"])
    return worktree_path(git_repo, "T-3")


def test_commit_all_concludes_merge_resolved_by_editing(git_repo):
    # arrange
    p = conflicted_worktree(git_repo)
    (p / "a.txt").write_text("one and two\n")
    # act
    sha = commit_all(p, "T-3 title")
    # assert
    assert sha
    assert len(run_git(p, "log", "-1", "--format=%P").split()) == 2
    assert run_git(p, "log", "-1", "--format=%s").strip() == "T-3 title"
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=p).returncode != 0
    assert run_git(p, "status", "--porcelain").strip() == ""


def test_commit_all_refuses_merge_with_marker_lines_left(git_repo):
    # arrange
    p = conflicted_worktree(git_repo)
    (p / "a.txt").write_text("<<<<<<< HEAD\none\n=======\ntwo\n>>>>>>> dep-2\n")
    # act / assert
    with pytest.raises(GitError, match="a.txt"):
        commit_all(p, "T-3 title")
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=p).returncode == 0


def test_commit_all_merge_ignores_marker_like_lines_in_unchanged_files(git_repo):
    # arrange
    commit_file(git_repo, "doc.txt", ">>>>>>> example from docs\n")
    p = conflicted_worktree(git_repo)
    (p / "a.txt").write_text("resolved\n")
    # act
    sha = commit_all(p, "T-3 title")
    # assert
    assert sha


# --- ребейз с незакоммиченными изменениями (P8) ---

def test_rebase_carrying_changes_moves_dirty_work_onto_new_base(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    (p / "new.txt").write_text("n\n")
    commit_file(git_repo, "main.txt", "x")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(True, [])
    assert (p / "main.txt").exists()
    assert (p / "a.txt").read_text() == "mine\n" and (p / "new.txt").read_text() == "n\n"
    assert run_git(p, "status", "--porcelain").splitlines() == [" M a.txt", "?? new.txt"]
    assert run_git(p, "for-each-ref", "refs/agent-board/").strip() == ""
    assert run_git(p, "rev-parse", "HEAD") == run_git(git_repo, "rev-parse", "main")
    assert run_git(p, "stash", "list").strip() == ""


def test_rebase_carrying_changes_clean_tree(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    commit_file(git_repo, "main.txt", "x")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(True, [])
    assert (p / "main.txt").exists()


def test_rebase_carrying_changes_pop_conflict_leaves_markers_and_clean_index(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    (p / "new.txt").write_text("n\n")
    commit_file(git_repo, "a.txt", "theirs\n")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(True, ["a.txt"])
    text = (p / "a.txt").read_text()
    assert "<<<<<<< " in text and "mine" in text and "theirs" in text
    assert (p / "new.txt").read_text() == "n\n"
    assert run_git(p, "diff", "--name-only", "--diff-filter=U").strip() == ""


def test_rebase_carrying_changes_rebase_conflict_aborts_and_restores_changes(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    commit_all(p, "mine")
    head = run_git(p, "rev-parse", "HEAD").strip()
    (p / "wip.txt").write_text("w\n")
    (p / "a.txt").write_text("mine more\n")
    commit_file(git_repo, "a.txt", "theirs\n")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(False, [])
    assert run_git(p, "rev-parse", "HEAD").strip() == head
    assert (p / "wip.txt").read_text() == "w\n" and (p / "a.txt").read_text() == "mine more\n"
    assert run_git(p, "stash", "list").strip() == ""


def test_commit_all_refuses_markers_left_by_stash_pop_conflict(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    commit_file(git_repo, "a.txt", "theirs\n")
    assert rebase_carrying_changes(p, "main", carry_ref("T-1")) == RebaseOutcome(True, ["a.txt"])
    # act / assert
    with pytest.raises(GitError, match="a.txt"):
        commit_all(p, "T-1 title")


def test_rebase_carrying_changes_keeps_work_in_ref_when_restore_fails_without_conflict(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    (p / "shared.txt").write_text("моя версия\n")
    run_git(git_repo, "stash", "list")
    commit_file(git_repo, "shared.txt", "из зависимости\n")
    stashes_before = run_git(git_repo, "stash", "list")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(True, [], carried_ref="refs/agent-board/T-1/carry")
    assert run_git(p, "rev-parse", "HEAD") == run_git(git_repo, "rev-parse", "main")
    saved = outcome.carried_ref
    assert run_git(p, "show", saved + ":a.txt") == "mine\n"
    assert run_git(p, "show", saved + "^3:shared.txt") == "моя версия\n"
    assert run_git(git_repo, "stash", "list") == stashes_before == ""


def test_rebase_carrying_changes_failed_restore_resets_tree_to_rebased_head(git_repo):
    # arrange: отслеживаемая правка вернётся, а неотслеживаемый файл столкнётся с файлом из новой базы
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    (p / "shared.txt").write_text("моя версия\n")
    commit_file(git_repo, "shared.txt", "из зависимости\n")
    # act
    outcome = rebase_carrying_changes(p, "main", carry_ref("T-1"))
    # assert
    assert outcome == RebaseOutcome(True, [], carried_ref="refs/agent-board/T-1/carry")
    assert run_git(p, "status", "--porcelain") == ""
    assert (p / "a.txt").read_text() == "base\n" and (p / "shared.txt").read_text() == "из зависимости\n"
    assert run_git(p, "show", outcome.carried_ref + ":a.txt") == "mine\n"
    assert run_git(p, "show", outcome.carried_ref + "^3:shared.txt") == "моя версия\n"


def test_rebase_carrying_changes_refuses_when_previous_carry_not_resolved(git_repo):
    # arrange
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    run_git(p, "update-ref", carry_ref("T-1"), "HEAD")
    (p / "a.txt").write_text("mine\n")
    commit_file(git_repo, "main.txt", "x")
    head = run_git(p, "rev-parse", "HEAD")
    # act / assert
    with pytest.raises(GitError, match="carry"):
        rebase_carrying_changes(p, "main", carry_ref("T-1"))
    assert run_git(p, "rev-parse", "HEAD") == head and (p / "a.txt").read_text() == "mine\n"


def _start_conflicting_rebase(git_repo):
    p = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    (p / "a.txt").write_text("mine\n")
    commit_all(p, "mine")
    commit_file(git_repo, "a.txt", "theirs\n")
    res = subprocess.run(["git", "rebase", "main"], cwd=str(p), capture_output=True, text=True)
    assert res.returncode != 0
    return p


@pytest.mark.parametrize("call", [
    lambda p: rebase_onto(p, "main"),
    lambda p: rebase_carrying_changes(p, "main", carry_ref("T-1")),
])
def test_rebase_refuses_when_rebase_already_in_progress(git_repo, call):
    # arrange
    p = _start_conflicting_rebase(git_repo)
    # act / assert
    with pytest.raises(GitError, match="rebase"):
        call(p)
    assert any((p / ".." / ".." / ".git" / "worktrees" / "T-1" / name).resolve().exists()
               for name in ("rebase-merge", "rebase-apply"))
