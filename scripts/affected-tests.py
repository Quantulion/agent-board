#!/usr/bin/env python3
"""Печатает файлы pytest-тестов, которые затрагивают изменения в репозитории.

Изменения берутся из git: незакоммиченные правки и новые файлы относительно HEAD
или, с --base, всё, чем рабочее дерево отличается от указанной ветки. Можно передать
пути файлов явно. По каждому файлу берутся тесты из карты RULES; изменённый файл
тестов запускается сам. Если для файла правила нет или он задевает весь набор
(conftest, фейки, pyproject), печатается каталог всех тестов.

Пути печатаются по одному на строку относительно текущего каталога:

    cd board && python -m pytest -q $(python ../scripts/affected-tests.py)
    python3 scripts/affected-tests.py --base main
    python3 scripts/affected-tests.py board/agent_board/runner.py
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = "board/tests"
ALL = "*"

# Файл (шаблон пути от корня репозитория) → тесты, которые его проверяют.
# Помимо карты к модулю пакета добавляются файлы тестов, которые импортируют его напрямую.
# Срабатывает первое подходящее правило.
RULES = [
    ("board/tests/conftest.py", ALL),
    ("board/tests/home_guard.py", ALL),
    ("board/tests/fakes/*", ALL),
    ("board/tests/fixtures/*", ["test_harness.py"]),
    ("board/tests/skill/scenarios/*", ["skill/"]),
    ("board/tests/js/*", []),
    ("board/agent_board/__init__.py", ["test_packaging.py", "test_license.py", "test_install.py", "test_cli.py",
                                       "test_server.py", "skill/test_wrapper.py", "skill/test_packaging_skill.py"]),
    ("board/agent_board/__main__.py", ["test_e2e.py", "test_runner.py"]),
    ("board/agent_board/board.py", ["test_board.py", "test_proposals.py", "test_concurrency.py", "test_server.py",
                                    "test_cli.py", "test_runner.py", "skill/test_skill_text.py"]),
    ("board/agent_board/cli.py", ["test_cli.py", "test_home_guard.py", "test_e2e.py", "skill/"]),
    ("board/agent_board/config.py", ["test_board.py", "test_model.py", "test_cli.py", "test_server.py"]),
    ("board/agent_board/fake_agent.py", ["test_runner.py", "test_e2e.py", "test_harness.py"]),
    ("board/agent_board/graph.py", ["test_graph.py", "test_board.py", "test_runner.py"]),
    ("board/agent_board/harness.py", ["test_harness.py", "test_runner.py", "test_e2e.py"]),
    ("board/agent_board/install.py", ["test_install.py", "skill/test_wrapper.py"]),
    ("board/agent_board/lifecycle.py", ["test_cli.py", "test_install.py", "skill/test_wrapper.py"]),
    ("board/agent_board/messages.py", ["test_messages.py"]),
    ("board/agent_board/model.py", ["test_model.py", "test_board.py", "test_store.py"]),
    ("board/agent_board/projects.py", ["test_projects.py", "test_server.py", "test_cli.py"]),
    ("board/agent_board/fspaths.py", ["test_fspaths.py", "test_board.py", "test_server.py"]),
    ("board/agent_board/prompts.py", ["test_prompts.py", "test_runner.py"]),
    ("board/agent_board/prompts/*", ["test_prompts.py", "test_packaging.py"]),
    ("board/agent_board/resources.py", ["test_resources.py", "test_server.py"]),
    ("board/agent_board/review.py", ["test_review.py", "test_runner.py", "test_e2e.py"]),
    ("board/agent_board/runner.py", ["test_runner.py", "test_e2e.py", "test_harness.py", "test_resources.py"]),
    ("board/agent_board/schema.py", ["test_server.py", "test_cli.py"]),
    ("board/agent_board/server.py", ["test_server.py", "test_cli.py", "test_e2e.py"]),
    ("board/agent_board/settings.py", ["test_settings.py", "test_board.py", "test_projects.py", "test_server.py",
                                       "test_cli.py", "test_runner.py"]),
    ("board/agent_board/ship.py", ["test_ship.py", "test_runner.py", "test_server.py"]),
    ("board/agent_board/store.py", ["test_store.py", "test_board.py", "test_proposals.py", "test_concurrency.py"]),
    ("board/agent_board/tracker.py", ["test_tracker.py", "test_server.py", "test_cli.py", "test_e2e.py"]),
    ("board/agent_board/wakes.py", ["test_runner.py"]),
    ("board/agent_board/worktree.py", ["test_worktree.py", "test_runner.py"]),
    ("board/agent_board/static/*", ["test_server.py", "test_packaging.py"]),
    ("skills/*", ["skill/"]),
    (".claude-plugin/*", ["test_packaging.py", "test_license.py", "skill/test_packaging_skill.py"]),
    ("scripts/install.py", ["test_install.py"]),
    ("scripts/install-codex.sh", ["skill/test_packaging_skill.py"]),
    ("scripts/export-repo.py", ["test_export_repo.py"]),
    ("scripts/affected-tests.py", ["test_affected_tests.py"]),
    ("scripts/test-parallel.py", ["test_test_parallel.py"]),
    ("README.md", ["test_license.py", "test_docs.py", "skill/test_packaging_skill.py"]),
    ("LICENSE", ["test_license.py"]),
    ("pyproject.toml", ALL),
    ("docs/*", ["test_docs.py"]),
    ("ui-tests/*", []),
    ("reports/*", []),
    (".gitignore", ["test_export_repo.py"]),
]
# Проверка публикуемости смотрит на все отслеживаемые файлы и стоит доли секунды.
ALWAYS = ["test_publishability.py"]


def changed_files(base):
    """Пути изменённых и новых файлов относительно корня репозитория."""
    def git(*args):
        out = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, check=True).stdout
        return [p for p in out.decode("utf-8").split("\0") if p]
    return sorted(set(git("diff", "--name-only", "-z", base or "HEAD")
                      + git("ls-files", "-z", "--others", "--exclude-standard")))


def _importers(module, tests_dir):
    """Файлы тестов, которые импортируют модуль пакета напрямую."""
    pattern = re.compile(r"(from|import)\s+agent_board\.%s\b|from\s+agent_board\s+import\s+[^\n]*\b%s\b"
                         % (module, module))
    found = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
            found.append(path.relative_to(tests_dir).as_posix())
    return found


def affected(paths, root=ROOT):
    """Файлы и каталоги тестов (относительно board/tests) для изменённых путей; ALL — весь набор."""
    tests_dir = root / TESTS
    result = set()
    for rel in paths:
        name = rel.rsplit("/", 1)[-1]
        if rel.startswith(TESTS + "/") and fnmatch.fnmatch(name, "test_*.py"):
            result.add(rel[len(TESTS) + 1:])
            continue
        rule = next((tests for pattern, tests in RULES if fnmatch.fnmatch(rel, pattern)), None)
        if rule is None or rule == ALL:
            return ALL
        result.update(rule)
        if rel.startswith("board/agent_board/") and rel.endswith(".py") and "/" not in rel[len("board/agent_board/"):]:
            result.update(_importers(name[:-3], tests_dir))
    if paths:
        result.update(ALWAYS)
    # каталог поглощает свои файлы
    dirs = {t for t in result if t.endswith("/")}
    result = {t for t in result if not any(t != d and t.startswith(d) for d in dirs)}
    # удалённые файлы тестов запускать не нужно
    return sorted(t for t in result if (tests_dir / t).exists())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Файлы тестов, которые затрагивают изменения.")
    parser.add_argument("files", nargs="*", help="пути от корня репозитория; по умолчанию изменения из git")
    parser.add_argument("--base", help="ветка или коммит, от которого считать изменения (по умолчанию HEAD)")
    args = parser.parse_args(argv)
    paths = args.files or changed_files(args.base)
    tests = affected(paths)
    targets = [TESTS] if tests == ALL else [TESTS + "/" + t for t in tests]
    for target in targets:
        print(os.path.relpath(str(ROOT / target)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
