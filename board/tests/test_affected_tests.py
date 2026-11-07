from __future__ import annotations

import fnmatch
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "affected-tests.py"

_spec = importlib.util.spec_from_file_location("affected_tests", str(SCRIPT))
affected_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(affected_tests)


def test_affected__package_module__mapped_tests_and_direct_importers():
    # act
    tests = affected_tests.affected(["board/agent_board/runner.py"])

    # assert
    assert {"test_runner.py", "test_e2e.py", "test_harness.py", "test_publishability.py"} <= set(tests)
    assert "test_board.py" not in tests


def test_affected__test_file_changed__runs_itself():
    # act
    tests = affected_tests.affected(["board/tests/test_graph.py"])

    # assert
    assert tests == ["test_graph.py", "test_publishability.py"]


def test_affected__shared_fixture_or_unknown_file__whole_suite():
    # act
    results = [affected_tests.affected([path]) for path in
               ["board/tests/conftest.py", "pyproject.toml", "board/agent_board/new_module.py", "Makefile"]]

    # assert
    assert results == [affected_tests.ALL] * 4


def test_affected__directory_and_its_files__directory_only():
    # act
    tests = affected_tests.affected(["skills/agent-board/SKILL.md", "board/tests/skill/test_wrapper.py"])

    # assert
    assert tests == ["skill/", "test_publishability.py"]


def test_affected__docs_only__only_publishability():
    # act
    tests = affected_tests.affected(["docs/superpowers/specs/x.md"])

    # assert
    assert tests == ["test_publishability.py"]


def test_rules__every_package_module_has_rule_and_targets_exist():
    # arrange
    tests_dir = ROOT / "board" / "tests"
    modules = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "board" / "agent_board").glob("*.py"))

    # act
    unmapped = [m for m in modules if not any(fnmatch.fnmatch(m, pattern) for pattern, _ in affected_tests.RULES)]
    missing = [t for _, tests in affected_tests.RULES if tests != affected_tests.ALL for t in tests
               if not (tests_dir / t).exists()]

    # assert
    assert unmapped == []
    assert missing == []


def test_main__uncommitted_change_in_git__prints_paths_from_cwd(git_repo):
    # arrange
    (git_repo / "scripts").mkdir()
    shutil.copy(str(SCRIPT), str(git_repo / "scripts" / "affected-tests.py"))
    tests_dir = git_repo / "board" / "tests"
    tests_dir.mkdir(parents=True)
    for name in ["test_graph.py", "test_board.py", "test_runner.py", "test_publishability.py"]:
        (tests_dir / name).write_text("")
    (git_repo / "board" / "agent_board").mkdir()
    (git_repo / "board" / "agent_board" / "graph.py").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-qm", "layout"], cwd=str(git_repo), check=True)
    (git_repo / "board" / "agent_board" / "graph.py").write_text("x = 1\n")

    # act
    r = subprocess.run([sys.executable, str(git_repo / "scripts" / "affected-tests.py")],
                       cwd=str(git_repo / "board"), capture_output=True, text=True, timeout=30)

    # assert
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["tests/test_board.py", "tests/test_graph.py", "tests/test_publishability.py",
                                "tests/test_runner.py"]
