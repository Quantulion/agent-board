from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = ROOT / "scripts" / "install.py"
SKILL_WRAPPER = ROOT / "skills" / "agent-board" / "scripts" / "agent-board"
PACKAGE = ROOT / "board" / "agent_board"

CONFLICT = "<<<<<<< HEAD\nбыло\n=======\nстало\n>>>>>>> other\n"


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True, env=env, check=True)
    return r.stdout.strip()


def set_version(repo: Path, version: str) -> None:
    init = repo / "board" / "agent_board" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    lines = [('__version__ = "%s"' % version) if line.startswith("__version__") else line
             for line in text.splitlines()]
    init.write_text("\n".join(lines) + "\n", encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    """Временный репозиторий с текущим кодом пакета на ветке main."""
    path = tmp_path / "repo"
    shutil.copytree(str(PACKAGE), str(path / "board" / "agent_board"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    git(path, "init", "-q", "-b", "main")
    set_version(path, "1.0.0")
    commit(path, "first")
    return path


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


def run(cmd, home: Path, cwd: Path, env=None):
    full = dict(os.environ, AGENT_BOARD_HOME=str(home))
    full.pop("PYTHONPATH", None)
    full.update(env or {})
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=60, cwd=str(cwd), env=full)


def install(repo: Path, home: Path, *extra: str):
    return run([sys.executable, INSTALL_SCRIPT, "--repo", repo, "--home", home] + list(extra), home, repo)


def wrapper(home: Path) -> Path:
    return home / "bin" / "agent-board"


def break_working_tree(repo: Path) -> None:
    """Рабочее дерево посреди влива с конфликтом и с незакоммиченными правками."""
    pkg = repo / "board" / "agent_board"
    with (pkg / "cli.py").open("a", encoding="utf-8") as f:
        f.write(CONFLICT)
    set_version(repo, "9.9.9-dirty")


def test_install__dirty_working_tree__wrapper_runs_committed_code(repo, home):
    # arrange
    break_working_tree(repo)

    # act
    installed = install(repo, home)
    from_repo_root = run([wrapper(home), "version"], home, repo)
    from_package_parent = run([wrapper(home), "version"], home, repo / "board")
    help_text = run([wrapper(home), "--help"], home, repo / "board")

    # assert
    assert installed.returncode == 0, installed.stderr
    assert from_repo_root.returncode == 0 and from_repo_root.stdout.strip() == "1.0.0", from_repo_root.stderr
    assert from_package_parent.stdout.strip() == "1.0.0", from_package_parent.stderr
    assert help_text.returncode == 0 and "self-update" in help_text.stdout


def test_install__default_ref__takes_main_not_checked_out_branch(repo, home):
    # arrange
    main = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    set_version(repo, "2.0.0-feature")
    commit(repo, "feature")

    # act
    installed = install(repo, home)

    # assert
    assert installed.returncode == 0, installed.stderr
    assert run([wrapper(home), "version"], home, repo).stdout.strip() == "1.0.0"
    assert json.loads((home / "runtime" / "install.json").read_text(encoding="utf-8"))["commit"] == main
    assert (home / "runtime" / main / "agent_board" / "__main__.py").is_file()


def test_self_update__new_commit__switches_wrapper_and_keeps_old_runtime(repo, home):
    # arrange
    first = git(repo, "rev-parse", "HEAD")
    assert install(repo, home).returncode == 0
    set_version(repo, "1.1.0")
    second = commit(repo, "second")
    break_working_tree(repo)

    # act
    updated = run([wrapper(home), "self-update"], home, repo)

    # assert
    assert updated.returncode == 0, updated.stderr
    assert second[:12] in updated.stdout
    assert run([wrapper(home), "version"], home, repo).stdout.strip() == "1.1.0"
    assert str(home / "runtime" / second) in wrapper(home).read_text(encoding="utf-8")
    assert (home / "runtime" / first / "agent_board").is_dir()


def test_self_update__same_commit__reports_unchanged(repo, home):
    # arrange
    assert install(repo, home).returncode == 0

    # act
    again = run([wrapper(home), "self-update"], home, repo)

    # assert
    assert again.returncode == 0 and "уже установлен" in again.stdout


def test_self_update__explicit_ref__installs_that_commit(repo, home):
    # arrange
    first = git(repo, "rev-parse", "HEAD")
    set_version(repo, "1.1.0")
    commit(repo, "second")
    assert install(repo, home).returncode == 0

    # act
    updated = run([wrapper(home), "self-update", "--ref", first], home, repo)

    # assert
    assert updated.returncode == 0, updated.stderr
    assert run([wrapper(home), "version"], home, repo).stdout.strip() == "1.0.0"


def test_self_update__unknown_ref__fails_and_keeps_current_install(repo, home):
    # arrange
    assert install(repo, home).returncode == 0
    before = wrapper(home).read_text(encoding="utf-8")

    # act
    r = run([wrapper(home), "self-update", "--ref", "no-such-branch"], home, repo)

    # assert
    assert r.returncode == 1 and "no-such-branch" in r.stderr
    assert wrapper(home).read_text(encoding="utf-8") == before


def test_install__wrapper_without_board_home_env__uses_install_home(repo, home, tmp_path):
    # arrange
    assert install(repo, home).returncode == 0
    env = {k: v for k, v in os.environ.items() if k != "AGENT_BOARD_HOME" and k != "PYTHONPATH"}

    # act
    r = subprocess.run([str(wrapper(home)), "status", "--json"], capture_output=True, text=True, timeout=60,
                       cwd=str(tmp_path), env=env)

    # assert
    assert r.returncode == 0 and json.loads(r.stdout) == {"server": None, "projects": []}, r.stderr


def test_skill_wrapper__installed__runs_installed_code(repo, home):
    # arrange
    assert install(repo, home).returncode == 0
    break_working_tree(repo)

    # act
    r = run([SKILL_WRAPPER, "version"], home, repo)
    from_repo = run([SKILL_WRAPPER, "version"], home, repo, env={"AGENT_BOARD_FROM_REPO": "1"})

    # assert
    assert r.returncode == 0 and r.stdout.strip() == "1.0.0", r.stderr
    assert from_repo.stdout.strip() != "1.0.0"


def test_install__serve_detach_from_dirty_tree__runs_installed_code_and_stops(repo, home):
    # arrange
    assert install(repo, home).returncode == 0
    break_working_tree(repo)
    flags = ["serve", "--detach", "--no-runner", "--no-tracker-sync"]

    try:
        # act
        started = run([wrapper(home)] + flags, home, repo / "board")
        info = json.loads((home / "server.json").read_text(encoding="utf-8"))
        stopped = run([wrapper(home), "stop"], home, repo)

        # assert
        assert started.returncode == 0, started.stderr
        assert info["version"] == "1.0.0"
        assert stopped.returncode == 0 and not (home / "server.json").exists(), stopped.stderr
    finally:
        try:
            os.kill(json.loads((home / "server.json").read_text(encoding="utf-8"))["pid"], 9)
        except (OSError, ValueError, KeyError):
            pass
