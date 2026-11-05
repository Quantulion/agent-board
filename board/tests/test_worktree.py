from __future__ import annotations

import subprocess

import pytest

from agent_board.worktree import (
    GitError,
    changed_files,
    commit_all,
    diff,
    ensure_worktree,
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
