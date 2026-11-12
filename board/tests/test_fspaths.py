from __future__ import annotations

import subprocess

from agent_board import fspaths


def git_init(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True, capture_output=True)
    return path


def test_complete__name_prefix__only_matching_dirs(tmp_path):
    # arrange
    git_init(tmp_path / "api")
    (tmp_path / "app").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "apx.txt").write_text("")
    # act
    items = fspaths.complete(str(tmp_path) + "/ap")
    # assert
    assert items == [
        {"path": str(tmp_path) + "/api", "abs": str(tmp_path / "api"), "git": True, "branch": "main"},
        {"path": str(tmp_path) + "/app", "abs": str(tmp_path / "app"), "git": False, "branch": None},
    ]


def test_complete__tilde__expanded_and_kept_in_path(tmp_path, monkeypatch):
    # arrange
    monkeypatch.setenv("HOME", str(tmp_path))
    git_init(tmp_path / "code" / "api")
    # act
    items = fspaths.complete("~/code/")
    # assert
    assert items == [{"path": "~/code/api", "abs": str(tmp_path / "code" / "api"), "git": True, "branch": "main"}]


def test_complete__bare_tilde__lists_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "code").mkdir()
    assert [i["path"] for i in fspaths.complete("~")] == ["~/code"]


def test_complete__hidden_dirs__shown_only_for_dot_prefix(tmp_path):
    # arrange
    (tmp_path / ".cache").mkdir()
    (tmp_path / "src").mkdir()
    # act
    plain = fspaths.complete(str(tmp_path) + "/")
    dotted = fspaths.complete(str(tmp_path) + "/.")
    # assert
    assert [i["path"] for i in plain] == [str(tmp_path) + "/src"]
    assert [i["path"] for i in dotted] == [str(tmp_path) + "/.cache"]


def test_complete__limit__respected(tmp_path):
    for i in range(5):
        (tmp_path / ("d%d" % i)).mkdir()
    assert [i["path"] for i in fspaths.complete(str(tmp_path) + "/", limit=3)] == \
        [str(tmp_path) + "/d0", str(tmp_path) + "/d1", str(tmp_path) + "/d2"]


def test_complete__missing_parent_or_relative__empty(tmp_path):
    assert fspaths.complete(str(tmp_path) + "/nope/x") == []
    assert fspaths.complete("relative/path") == []
    assert fspaths.complete("") == []


def test_default_branch__no_remote__current_branch(tmp_path):
    assert fspaths.default_branch(str(git_init(tmp_path / "r", branch="trunk"))) == "trunk"


def test_default_branch__origin_head__remote_default(tmp_path):
    # arrange: origin указывает на ветку по умолчанию, отличную от текущей
    repo = git_init(tmp_path / "r", branch="feature")
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo,
                   check=True, capture_output=True)
    # act / assert
    assert fspaths.default_branch(str(repo)) == "main"


def test_work_tree_root__subdir_and_not_git(tmp_path):
    # arrange
    repo = git_init(tmp_path / "r")
    (repo / "sub").mkdir()
    (tmp_path / "plain").mkdir()
    # act / assert
    assert fspaths.work_tree_root(str(repo / "sub")) == str(repo.resolve())
    assert fspaths.work_tree_root(str(tmp_path / "plain")) is None
