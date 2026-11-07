from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test-parallel.py"

_spec = importlib.util.spec_from_file_location("test_parallel", str(SCRIPT))
test_parallel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_parallel)


def test_make_units__split_file_by_tests_other_files_whole():
    # arrange
    nodeids = ["board/tests/test_a.py::t1", "board/tests/test_runner.py::r1", "board/tests/test_a.py::t2",
               "board/tests/skill/test_s.py::s1", "board/tests/test_runner.py::r2"]

    # act
    units = test_parallel.make_units(nodeids)

    # assert
    assert units == [["board/tests/test_a.py::t1", "board/tests/test_a.py::t2"], ["board/tests/test_runner.py::r1"],
                     ["board/tests/skill/test_s.py::s1"], ["board/tests/test_runner.py::r2"]]


def test_balance__heaviest_unit_goes_to_lightest_group():
    # arrange
    units = [["a"], ["b"], ["c"], ["d"]]
    durations = {"a": 5.0, "b": 4.0, "c": 3.0, "d": 2.0}

    # act
    groups = test_parallel.balance(units, 2, durations)

    # assert
    assert [(g["nodeids"], g["seconds"]) for g in groups] == [(["a", "d"], 7.0), (["b", "c"], 7.0)]


def test_balance__more_workers_than_units__no_empty_groups():
    # act
    groups = test_parallel.balance([["a"]], 4, {})

    # assert
    assert [g["nodeids"] for g in groups] == [["a"]]


def test_read_junit__maps_cases_to_nodeids_with_failures_and_skips(tmp_path):
    # arrange
    nodeids = ["board/tests/skill/test_s.py::test_ok[x-1]", "board/tests/test_a.py::TestCls::test_bad",
               "board/tests/test_a.py::test_skip"]
    junit = tmp_path / "r.xml"
    junit.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="board.tests.skill.test_s" name="test_ok[x-1]" time="1.5"/>'
        '<testcase classname="board.tests.test_a.TestCls" name="test_bad" time="0.5"><failure/></testcase>'
        '<testcase classname="board.tests.test_a.TestCls" name="test_bad" time="0.1"><error/></testcase>'
        '<testcase classname="board.tests.test_a" name="test_skip" time="0"><skipped/></testcase>'
        '</testsuite></testsuites>', encoding="utf-8")

    # act
    durations, failed, skipped = test_parallel.read_junit(junit, {test_parallel.junit_key(n): n for n in nodeids})

    # assert
    assert durations == {nodeids[0]: 1.5, nodeids[1]: 0.6, nodeids[2]: 0.0}
    assert failed == {nodeids[1]} and skipped == {nodeids[2]}


def write_project(root):
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["default_port: порт по умолчанию"]\n', encoding="utf-8")
    (root / "test_ok.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', [1, 2, 3])\ndef test_ok(n):\n    assert n\n\n"
        "@pytest.mark.default_port\ndef test_port():\n    pass\n", encoding="utf-8")
    (root / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n\ndef test_good():\n    pass\n",
                                      encoding="utf-8")


def run_script(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "-n", "2"] + list(args),
                          cwd=str(root), capture_output=True, text=True, timeout=120)


def test_script__failing_test__exit_1_and_lists_failure(tmp_path):
    # arrange
    write_project(tmp_path)

    # act
    r = run_script(tmp_path)

    # assert
    assert r.returncode == 1, r.stdout + r.stderr
    assert "собрано 6, прогнано 6 — прошло 5, упало 1" in r.stdout
    assert "УПАЛ test_bad.py::test_bad" in r.stdout and "1 последовательно" in r.stdout


def test_script__all_green__exit_0_and_same_count_as_pytest(tmp_path):
    # arrange
    write_project(tmp_path)

    # act
    r = run_script(tmp_path, "--", "-k", "test_ok")

    # assert
    assert r.returncode == 0, r.stdout + r.stderr
    assert "собрано 4, прогнано 4 — прошло 4, упало 0" in r.stdout
    cache = json.loads((tmp_path / test_parallel.CACHE).read_text(encoding="utf-8"))
    assert set(cache) == {"test_ok.py::test_ok[1]", "test_ok.py::test_ok[2]", "test_ok.py::test_ok[3]",
                          "test_ok.py::test_port"}
