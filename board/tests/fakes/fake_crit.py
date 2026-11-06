"""Поддельный crit для тестов: тот же интерфейс командной строки и вывода, что у crit 0.20 (плагин 1.8.x).

Настоящий crit поднимает демон (страница ревью) и клиент, который блокируется до «Finish Review».
Здесь демон — файл состояния в каталоге данных (`-o`), а «Finish Review» тест нажимает функцией finish().

Вывод клиента как у настоящего:
  stderr: «Started crit daemon at http://localhost:<port> (session <id>, PID <n>)» при новом демоне,
          «Connected to crit daemon at http://localhost:<port> (session <id>)» при подключении к живому,
          «no review found for session <id>» (код 1), если с --session демона нет;
          по завершении раунда — «approved: true|false».
  stdout: текст для агента; при остановке демона до завершения —
          «crit daemon shut down before review was finished.» (и approved: false, код 0).
SIGTERM клиента останавливает и демон (как у настоящего), SIGKILL — нет.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

STATE = "fake-crit"


# --- помощники для тестов ---

def install(bin_dir: Path) -> Path:
    """Исполняемый `crit` в bin_dir, запускающий этот модуль тем же интерпретатором."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "crit"
    path.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, Path(__file__).resolve()), encoding="utf-8")
    path.chmod(0o755)
    return path


def state_dir(data_dir) -> Path:
    path = Path(data_dir) / STATE
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure(data_dir, **cfg) -> None:
    """session — id сессии вместо вычисленного по каталогу; delay — пауза перед выводом ссылки; port."""
    _write(state_dir(data_dir) / "config.json", cfg)


def session_for(cwd) -> str:
    return hashlib.sha1(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:12]


def finish(data_dir, session: str, comments=(), approved: Optional[bool] = None) -> None:
    """Пользователь нажал «Finish Review», оставив comments ({path, line, body}; без path — общий комментарий)."""
    _write(state_dir(data_dir) / ("finish-%s.json" % session), {"comments": list(comments), "approved": approved})


def kill_daemon(data_dir, session: str) -> None:
    (state_dir(data_dir) / ("kill-%s" % session)).write_text("", encoding="utf-8")


def stop_daemon(data_dir, session: str) -> None:
    """Страница ревью закрылась без клиента (например, после перезагрузки машины); комментарии остаются."""
    try:
        (state_dir(data_dir) / ("daemon-%s.json" % session)).unlink()
    except OSError:
        pass


def daemon(data_dir, session: str) -> Optional[dict]:
    return _read(state_dir(data_dir) / ("daemon-%s.json" % session))


def calls(data_dir) -> List[dict]:
    path = state_dir(data_dir) / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def review_calls(data_dir) -> List[dict]:
    """Запуски клиента раунда (без подкоманд comments/comment/status)."""
    return [c for c in calls(data_dir) if not c["argv"] or c["argv"][0].startswith("-")]


def comments(data_dir, session: str) -> List[dict]:
    return _read(state_dir(data_dir) / ("comments-%s.json" % session)) or []


# --- реализация ---

def _write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _opt(argv: List[str], *names: str) -> Optional[str]:
    for i, a in enumerate(argv):
        if a in names and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _data_dir(argv: List[str]) -> Path:
    raw = _opt(argv, "-o", "--output")
    return Path(raw) if raw else Path.home() / ".crit"


def _config(data: Path) -> dict:
    return _read(state_dir(data) / "config.json") or {}


def _session(data: Path) -> str:
    return _config(data).get("session") or session_for(os.getcwd())


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_comment(item: dict, rnd: int) -> dict:
    path = item.get("path")
    line = item.get("line")
    if not path:
        scope, prefix = "review", "r_"
    elif line:
        scope, prefix = "line", "c_"
    else:
        scope, prefix = "file", "c_"
    c = {"scope": scope, "id": item.get("id") or prefix + "%06x" % random.randrange(16 ** 6),
         "start_line": int(line or 0), "end_line": int(line or 0), "body": item["body"],
         "author": item.get("author") or "Reviewer", "created_at": _now(), "updated_at": _now(), "review_round": rnd}
    if path:
        c["path"] = path
    if item.get("resolved"):
        c["resolved"] = True
    return c


def _unresolved(items: List[dict]) -> List[dict]:
    return [c for c in items if not c.get("resolved")]


def _review(argv: List[str]) -> int:
    data = _data_dir(argv)
    st = state_dir(data)
    cfg = _config(data)
    wanted = _opt(argv, "--session")
    session = wanted or _session(data)
    daemon_file = st / ("daemon-%s.json" % session)
    comments_file = st / ("comments-%s.json" % session)
    record = _read(daemon_file)
    if cfg.get("delay"):
        time.sleep(float(cfg["delay"]))
    if wanted and record is None:
        _err("no review found for session %s" % session)
        return 1
    if record is None:
        port = int(_opt(argv, "-p", "--port") or cfg.get("port") or random.randrange(40000, 60000))
        rnd = max([c.get("review_round", 1) for c in _read(comments_file) or []] + [0]) + 1
        record = {"session": session, "port": port, "round": rnd, "finished": False, "cwd": os.getcwd()}
        _write(daemon_file, record)
        _err("Started crit daemon at http://localhost:%d (session %s, PID %d)" % (port, session, os.getpid()))
    else:
        # повторный запуск после завершённого раунда сообщает «раунд готов» и открывает следующий
        if record.get("finished"):
            record.update(round=record["round"] + 1, finished=False)
            _write(daemon_file, record)
        _err("Connected to crit daemon at http://localhost:%d (session %s)" % (record["port"], session))

    def on_term(signum, frame):
        # как у настоящего: остановка клиента останавливает демон, файл ревью остаётся
        try:
            daemon_file.unlink()
        except OSError:
            pass
        os._exit(143)

    signal.signal(signal.SIGTERM, on_term)
    finish_file = st / ("finish-%s.json" % session)
    kill_file = st / ("kill-%s" % session)
    while True:
        if kill_file.exists():
            kill_file.unlink()
            try:
                daemon_file.unlink()
            except OSError:
                pass
            sys.stdout.write("crit daemon shut down before review was finished.\n")
            sys.stdout.flush()
            _err("approved: false")
            return 0
        if finish_file.exists():
            consumed = finish_file.with_name(finish_file.name + ".done%d" % os.getpid())
            try:
                os.replace(str(finish_file), str(consumed))
            except OSError:
                time.sleep(0.02)
                continue
            request = _read(consumed) or {}
            consumed.unlink()
            break
        time.sleep(0.03)
    record = _read(daemon_file) or record
    items = (_read(comments_file) or []) + [_new_comment(c, record["round"]) for c in request.get("comments") or []]
    open_items = _unresolved(items)
    approved = request.get("approved")
    if approved is None:
        approved = not open_items
    if approved:
        for path in (daemon_file, comments_file):
            try:
                path.unlink()
            except OSError:
                pass
        sys.stdout.write("Review approved with no comments — no changes requested.")
        sys.stdout.flush()
        _err("approved: true")
    else:
        _write(comments_file, items)
        record["finished"] = True
        _write(daemon_file, record)
        sys.stdout.write(
            "The review finished with %d unresolved comments.\n\n%s\n\n"
            "Address each comment. For each one, reply explaining what you did using "
            "`crit comment --reply-to <comment-id> --author <your-name> \"<explanation>\"`.\n\n"
            "When you're done, run:\n\n  crit --session %s\n" % (len(open_items), json.dumps(open_items), session))
        sys.stdout.flush()
        _err("approved: false")
    _err("\nSession: 1 files and 1s reviewed")
    return 0


def _comments(argv: List[str]) -> int:
    data = _data_dir(argv)
    items = _read(state_dir(data) / ("comments-%s.json" % _session(data))) or []
    shown = items if "--all" in argv else _unresolved(items)
    # общие комментарии первыми, как у настоящего
    shown = sorted(shown, key=lambda c: c.get("scope") != "review")
    if "--json" in argv:
        print(json.dumps(shown, indent=2, ensure_ascii=False))
    else:
        for c in shown:
            print("%s %s:%s %s" % (c["id"], c.get("path", ""), c.get("start_line", 0), c["body"]))
    return 0


def _comment(argv: List[str]) -> int:
    data = _data_dir(argv)
    path = state_dir(data) / ("comments-%s.json" % _session(data))
    items = _read(path) or []
    target = _opt(argv, "--reply-to")
    skip = {"--reply-to", "--author", "-o", "--output", "--path", "--session"}
    rest, i = [], 1
    while i < len(argv):
        if argv[i] in skip:
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    if not target or not rest:
        _err("fake crit: поддерживается только comment --reply-to <id> <body>")
        return 2
    for c in items:
        if c["id"] == target:
            c.setdefault("replies", []).append({"id": "rp_%06x" % random.randrange(16 ** 6), "body": rest[-1],
                                                "author": _opt(argv, "--author") or "", "created_at": _now()})
            _write(path, items)
            print("Replied to %s" % target)
            return 0
    _err("comment %s not found" % target)
    return 1


def _status(argv: List[str]) -> int:
    data = _data_dir(argv)
    session = _session(data)
    record = _read(state_dir(data) / ("daemon-%s.json" % session))
    review_file = str(data / "reviews" / session / "review.json")
    out = {"daemon": {"running": record is not None}, "review_file": review_file, "vcs": "git",
           "sessions": []}
    if record is not None:
        out["daemon"].update(port=record["port"])
        out["sessions"].append({"id": session, "port": record["port"], "review_file": review_file})
    print(json.dumps(out, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data = _data_dir(argv)
    with open(str(state_dir(data) / "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "pid": os.getpid(), "cwd": os.getcwd()}) + "\n")
    command = argv[0] if argv and not argv[0].startswith("-") else None
    if command is None:
        return _review(argv)
    if command == "comments":
        return _comments(argv)
    if command == "comment":
        return _comment(argv)
    if command == "status":
        return _status(argv)
    _err("fake crit: неизвестная команда %s" % command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
