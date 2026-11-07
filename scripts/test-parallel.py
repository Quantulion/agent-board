#!/usr/bin/env python3
"""Прогоняет pytest-тесты в нескольких процессах и сводит общий итог.

Набор собирается один раз (`pytest --collect-only`) и делится на группы, близкие по
времени: файл целиком уходит в одну группу, а тяжёлые файлы из SPLIT дробятся по
тестам. Время тестов берётся из прошлого прогона (кэш в .pytest_cache), без него —
из оценок EST_SECONDS. Тесты с маркером `default_port` занимают порт доски по
умолчанию и идут отдельной группой после остальных, по одному.

Код выхода ненулевой, если упал хоть один тест или прогнано не столько тестов,
сколько собрано. Запускать тем интерпретатором, в котором стоит pytest:

    .venv/bin/python scripts/test-parallel.py
    .venv/bin/python scripts/test-parallel.py -n 6 board/tests/test_server.py
    .venv/bin/python scripts/test-parallel.py -- -m "not slow" --tb=line
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERIAL_MARK = "default_port"
# Файлы (или каталоги), которые делятся по отдельным тестам: в них самые долгие тесты набора.
SPLIT = ("board/tests/test_runner.py", "board/tests/skill/")
# Оценка секунд на тест, пока нет замеров прошлого прогона.
EST_SECONDS = {
    "board/tests/skill/test_scenarios_check_state.py": 3.0,
    "board/tests/test_runner.py": 1.0,
    "board/tests/test_concurrency.py": 3.0,
    "board/tests/test_e2e.py": 2.0,
    "board/tests/test_install.py": 1.0,
    "board/tests/test_worktree.py": 0.6,
    "board/tests/skill/test_wrapper.py": 0.4,
}
DEFAULT_SECONDS = 0.05
CACHE = ".pytest_cache/test-parallel-durations.json"


def file_of(nodeid):
    return nodeid.split("::", 1)[0]


def is_split(nodeid, split=SPLIT):
    path = file_of(nodeid)
    return any(path == s or (s.endswith("/") and path.startswith(s)) for s in split)


def make_units(nodeids, split=SPLIT):
    """Единицы раздачи по группам: файл целиком или отдельный тест тяжёлого файла."""
    units, by_file = [], {}
    for nodeid in nodeids:
        if is_split(nodeid, split):
            units.append([nodeid])
        else:
            if file_of(nodeid) not in by_file:
                by_file[file_of(nodeid)] = []
                units.append(by_file[file_of(nodeid)])
            by_file[file_of(nodeid)].append(nodeid)
    return units


def weight(nodeid, durations):
    if nodeid in durations:
        return durations[nodeid]
    return EST_SECONDS.get(file_of(nodeid), DEFAULT_SECONDS)


def balance(units, workers, durations):
    """Раскладывает единицы по группам: самая тяжёлая — в самую лёгкую группу."""
    groups = [{"nodeids": [], "seconds": 0.0} for _ in range(max(1, workers))]
    sized = sorted(((sum(weight(n, durations) for n in u), u) for u in units), key=lambda x: -x[0])
    for seconds, unit in sized:
        group = min(groups, key=lambda g: g["seconds"])
        group["nodeids"].extend(unit)
        group["seconds"] += seconds
    return [g for g in groups if g["nodeids"]]


def junit_key(nodeid):
    """Пара (classname, name), под которой тест попадает в junit-отчёт pytest."""
    parts = nodeid.split("::")
    module = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    return ".".join([module.replace("/", ".")] + parts[1:-1]), parts[-1]


def read_junit(path, keys):
    """Итог группы по junit-отчёту: {nodeid: время}, упавшие и пропущенные тесты."""
    durations, failed, skipped = {}, set(), set()
    if not path.exists():
        return durations, failed, skipped
    for case in ET.parse(str(path)).getroot().iter("testcase"):
        nodeid = keys.get((case.get("classname", ""), case.get("name", "")))
        if nodeid is None:
            nodeid = "%s::%s" % (case.get("classname", ""), case.get("name", ""))
        durations[nodeid] = durations.get(nodeid, 0.0) + float(case.get("time") or 0)
        if case.find("failure") is not None or case.find("error") is not None:
            failed.add(nodeid)
        elif case.find("skipped") is not None:
            skipped.add(nodeid)
    return durations, failed, skipped


def pytest_cmd(python, args):
    return [python, "-m", "pytest", "-p", "no:cacheprovider"] + list(args)


def collect(python, root, args):
    r = subprocess.run(pytest_cmd(python, ["--collect-only", "-q"] + list(args)), cwd=str(root),
                       capture_output=True, text=True)
    nodeids = [line.strip() for line in r.stdout.splitlines() if "::" in line and not line.startswith(" ")]
    # 5 — ничего не собрано; это не ошибка, если тестов просто нет
    if r.returncode not in (0, 5):
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit("сбор тестов упал (код %d)" % r.returncode)
    return nodeids


def load_cache(root):
    try:
        return json.loads((root / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(root, durations):
    path = root / CACHE
    try:
        path.parent.mkdir(exist_ok=True)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        merged = dict(load_cache(root), **durations)
        path.write_text(json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=0), encoding="utf-8")
    except OSError:
        pass


def run_groups(python, root, groups, options, logdir, first_index=1):
    """Запускает группы одновременно и ждёт все. Возвращает [(группа, код, лог, junit, секунды)]."""
    running = []
    try:
        for i, group in enumerate(groups, first_index):
            log, junit = logdir / ("group-%d.log" % i), logdir / ("group-%d.xml" % i)
            cmd = pytest_cmd(python, ["-q", "--junitxml=%s" % junit] + options + group["nodeids"])
            with open(str(log), "w", encoding="utf-8") as out:
                proc = subprocess.Popen(cmd, cwd=str(root), stdout=out, stderr=subprocess.STDOUT)
            running.append((i, group, proc, log, junit, time.monotonic()))
            print("группа %d: %d тестов, оценка %.0f с" % (i, len(group["nodeids"]), group["seconds"]), flush=True)
        finished = {}
        while len(finished) < len(running):
            for i, group, proc, log, junit, started in running:
                if i not in finished and proc.poll() is not None:
                    finished[i] = (i, group, proc.returncode, log, junit, time.monotonic() - started)
            time.sleep(0.2)
        return [finished[r[0]] for r in running]
    finally:
        for _, _, proc, _, _, _ in running:
            if proc.poll() is None:
                proc.terminate()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Параллельный прогон pytest с общим итогом. Опции pytest передаются после --.")
    parser.add_argument("paths", nargs="*", help="файлы и каталоги тестов от корня репозитория (по умолчанию весь набор)")
    parser.add_argument("-n", "--workers", type=int, default=min(4, os.cpu_count() or 1),
                        help="число процессов pytest (по умолчанию 4: на машине могут идти тесты других агентов)")
    parser.add_argument("--plan", action="store_true", help="только показать группы, не запуская тесты")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    argv = list(sys.argv[1:] if argv is None else argv)
    options = []
    if "--" in argv:
        options = argv[argv.index("--") + 1:]
        argv = argv[:argv.index("--")]
    args = parser.parse_args(argv)
    root, python = Path(args.root), sys.executable

    started = time.monotonic()
    nodeids = collect(python, root, args.paths + options)
    if not nodeids:
        print("тестов не собрано")
        return 0
    serial_set = set(collect(python, root, args.paths + ["-m", SERIAL_MARK]))
    serial = [n for n in nodeids if n in serial_set]
    durations = load_cache(root)
    groups = balance(make_units([n for n in nodeids if n not in serial_set]), args.workers, durations)
    serial_group = {"nodeids": serial, "seconds": sum(weight(n, durations) for n in serial)}

    if args.plan:
        for i, g in enumerate(groups, 1):
            print("группа %d: %d тестов, оценка %.0f с" % (i, len(g["nodeids"]), g["seconds"]))
        if serial:
            print("последовательно: %d тестов, оценка %.0f с" % (len(serial), serial_group["seconds"]))
        return 0

    print("собрано %d тестов, %d групп%s" % (len(nodeids), len(groups),
                                             ", %d последовательно после них" % len(serial) if serial else ""),
          flush=True)
    logdir = Path(tempfile.mkdtemp(prefix="test-parallel-"))
    results = run_groups(python, root, groups, options, logdir)
    if serial:
        results += run_groups(python, root, [serial_group], options, logdir, first_index=len(groups) + 1)

    keys = {junit_key(n): n for n in nodeids}
    seen, failed, skipped, broken = {}, set(), set(), []
    for i, group, code, log, junit, seconds in results:
        times, bad, skip = read_junit(junit, keys)
        seen.update(times)
        failed |= bad
        skipped |= skip
        missing = len(set(group["nodeids"]) - set(times))
        print("группа %d: код %d, %d тестов, упало %d, %.0f с" % (i, code, len(times), len(bad), seconds))
        if code != 0 or bad or missing:
            broken.append((i, log))
    save_cache(root, {n: t for n, t in seen.items() if n in keys.values()})

    for i, log in broken:
        print("\n===== лог группы %d (%s) =====" % (i, log))
        print(log.read_text(encoding="utf-8", errors="replace").rstrip())

    lost = [n for n in nodeids if n not in seen]
    passed = len(seen) - len(failed) - len(skipped)
    print("\nитого: собрано %d, прогнано %d — прошло %d, упало %d, пропущено %d; по стене %.0f с"
          % (len(nodeids), len(seen), passed, len(failed), len(skipped), time.monotonic() - started))
    for nodeid in sorted(failed):
        print("УПАЛ %s" % nodeid)
    for nodeid in lost:
        print("НЕ ПРОГНАН %s" % nodeid)
    if broken or lost or failed:
        print("логи групп: %s" % logdir)
        return 1
    shutil.rmtree(str(logdir), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
