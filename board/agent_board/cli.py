from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from . import __version__, lifecycle
from .config import ProjectConfig
from .server import BoardServer, ServerAlreadyRunning, read_discovery
from .store import ProjectStore

LOCK_POLL_SECONDS = 2.0
NOT_RUNNING = "доска не запущена: python3 -m agent_board serve"


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class ExitWith(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _actor() -> str:
    return os.environ.get("AGENT_BOARD_ACTOR") or "agent"


def _user_actor() -> str:
    """Актор команд пользователя: человек в терминале — пользователь, агенты запускаются с AGENT_BOARD_ACTOR
    (в том числе пустым) и получают отказ доски."""
    return os.environ.get("AGENT_BOARD_ACTOR", "user") or "agent"


def _project(args) -> str:
    slug = args.project or os.environ.get("AGENT_BOARD_PROJECT")
    if not slug:
        raise CliError("Не указан проект: --project или $AGENT_BOARD_PROJECT")
    return slug


def _json_arg(raw: str, name: str):
    try:
        return json.loads(raw)
    except ValueError as e:
        raise CliError("Некорректный JSON в %s: %s" % (name, e))


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 10):
    info = read_discovery()
    if info is None:
        raise CliError(NOT_RUNNING, 2)
    headers = {"X-Board-Token": info["token"]}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (info["port"], path), data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8")).get("error") or "HTTP %d" % e.code
        except (ValueError, AttributeError):
            message = "HTTP %d" % e.code
        raise CliError(str(message))
    except urllib.error.URLError:
        raise CliError(NOT_RUNNING, 2)
    except socket.timeout:
        # соединение есть, ответа нет: сервер мог не закончить запрос
        raise CliError("Доска не ответила за %g с: запрос всё ещё выполняется на сервере — проверьте доску" % timeout, 2)


def _p(args, suffix: str) -> str:
    return "/api/p/%s/%s" % (_project(args), suffix)


def cmd_serve(args):
    if args.detach:
        result = lifecycle.start_detached(args.port, args.no_runner, args.no_tracker_sync)
        if "error" in result:
            raise CliError(result["error"], result["code"])
        print(result["url"], flush=True)
        return None
    server = BoardServer(port=args.port, runner=not args.no_runner, tracker_sync=not args.no_tracker_sync)
    try:
        server.start()
    except ServerAlreadyRunning as e:
        raise CliError(str(e), 3)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(server.url, flush=True)
    try:
        while not stop.wait(0.5):
            pass
    finally:
        server.stop()
    return None


def cmd_init(args):
    ProjectStore(args.slug).create(ProjectConfig(slug=args.slug, title=args.title))
    return {"slug": args.slug, "title": args.title}


def cmd_show(args):
    for task in _request("GET", _p(args, "state"))["tasks"]:
        if task["key"] == args.key:
            return task
    raise CliError("Неизвестная задача: %s" % args.key)


def cmd_state(args):
    return _request("GET", _p(args, "state"))


def cmd_version(args):
    print(__version__)
    return None


def cmd_stop(args):
    result = lifecycle.stop_server()
    if result["code"]:
        raise CliError(result["message"], result["code"])
    print(result["message"])
    return None


def cmd_status(args):
    if args.key is None and args.status is None:
        report = lifecycle.status_report()
        if args.json:
            return report
        print(lifecycle.format_status(report))
        return None
    if args.key is None or args.status is None:
        raise CliError("Использование: agent-board status (состояние доски) или status <KEY> <STATUS>")
    return _request("POST", _p(args, "tasks/%s/transition" % args.key), {"to": args.status, "actor": _actor()})


UPDATE_OPTIONS = [
    ("summary", "summary", False),
    ("todo", "todo_json", True),
    ("changed_files", "changed_files_json", True),
    ("report", "report", False),
    ("rule_violations", "rule_violations_json", True),
]


def cmd_update(args):
    fields = {}
    for field, attr, is_json in UPDATE_OPTIONS:
        value = getattr(args, attr)
        if value is None:
            continue
        fields[field] = _json_arg(value, "--" + attr.replace("_", "-")) if is_json else value
    return _request("POST", _p(args, "tasks/%s/update" % args.key), {"actor": _actor(), "fields": fields})


def cmd_ask(args):
    question = {"text": args.text, "options": args.option or [], "context": args.context or ""}
    if args.recommended is not None:
        question["recommended"] = args.recommended
    return _request("POST", _p(args, "tasks/%s/questions" % args.key), {"question": question})


def cmd_say(args):
    body = {"author": _actor(), "text": args.text}
    task = os.environ.get("AGENT_BOARD_TASK")
    if task:
        body["from_task"] = task
    return _request("POST", _p(args, "threads/%s/messages" % args.thread), body)


def cmd_thread(args):
    return _request("GET", _p(args, "threads/%s" % args.thread))


def cmd_add_task(args):
    task = _json_arg(args.json, "--json")
    return _request("POST", _p(args, "tasks"), {"task": task, "actor": _user_actor()})


def cmd_deps(args):
    deps = _json_arg(args.json, "--json")
    return _request("POST", _p(args, "tasks/%s/deps" % args.key), {"deps": deps, "actor": _user_actor()})


def cmd_stage(args):
    return _request("POST", _p(args, "stage"), {"stage": args.stage, "actor": _user_actor()})


def cmd_ship(args):
    return _request("POST", _p(args, "ship"), {"keys": args.keys, "actor": _user_actor()})


# заведение задач идёт синхронно: по запросу к трекеру на задачу и связь
TRACKER_TIMEOUT = 600


def cmd_tracker(args):
    result = _request("POST", _p(args, "tracker/" + args.action), {"actor": _user_actor()},
                      timeout=TRACKER_TIMEOUT)
    problems = [e["error"] for e in result.get("errors") or []]
    if args.action in ("check", "sync") and not result.get("ok"):
        problems.append(result.get("error") or "нет доступа к трекеру")
    if problems:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n".join(problems), file=sys.stderr)
        raise ExitWith(1)
    return result


def cmd_config(args):
    if args.action == "schema":
        from .schema import config_schema, format_schema

        schema = config_schema()
        if args.json:
            return schema
        print(format_schema(schema))
        return None
    actor = _user_actor()
    if args.action == "get":
        config = _request("GET", _p(args, "config?actor=" + urllib.parse.quote(actor)))["config"]
        if args.json:
            return config
        for name, value in config.items():
            print("%s: %s" % (name, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)))
        return None
    if args.action == "set":
        fields = _json_arg(args.fields_json, "--json")
        return _request("POST", _p(args, "config"), {"actor": actor, "fields": fields, "dry_run": args.dry_run})
    # validate: проверка сохранённого файла без изменений
    return _request("POST", _p(args, "config"), {"actor": actor, "fields": {}, "dry_run": True})


def cmd_propose(args):
    payload = _json_arg(args.json, "--json")
    return _request("POST", _p(args, "proposals"), {"kind": args.kind, "payload": payload, "reason": args.reason or ""})


def cmd_events(args):
    return _request("GET", _p(args, "events?since=%d" % args.since))


def cmd_lock_run(args):
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise CliError("Не указана команда: agent-board lock-run <ресурс> -- <команда...>")
    task = os.environ.get("AGENT_BOARD_TASK")
    if not task:
        raise CliError("Не задана переменная $AGENT_BOARD_TASK")
    interval = _lock_interval()
    body = {"task": task, "pid": os.getpid()}
    base = _p(args, "locks/%s/" % args.resource)
    previous = _interrupt_on_termination()
    queued = False
    child = None
    try:
        position = _lock_call(base + "acquire", body)
        queued = True
        shown = None
        while position > 0:
            if position != shown:
                print("ждёт ресурс %s: позиция %d" % (args.resource, position), file=sys.stderr, flush=True)
                shown = position
            time.sleep(interval)
            position = _lock_call(base + "poll", body)
        try:
            child = subprocess.Popen(cmd)
        except OSError as e:
            print("Не удалось запустить %s: %s" % (cmd[0], e), file=sys.stderr)
            raise ExitWith(127)
        raise ExitWith(child.wait())
    except KeyboardInterrupt:
        _stop_child(child)
        raise ExitWith(130)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if queued:
            try:
                _request("POST", base + "release", body)
            except CliError as e:
                print("Не удалось освободить ресурс %s: %s" % (args.resource, e), file=sys.stderr)


def _lock_interval() -> float:
    return float(os.environ.get("AGENT_BOARD_LOCK_POLL") or LOCK_POLL_SECONDS)


def _interrupt_on_termination() -> dict:
    """SIGTERM/SIGHUP превращаются в KeyboardInterrupt, чтобы отработали очистка и освобождение ресурса."""
    if threading.current_thread() is not threading.main_thread():
        return {}

    def handler(signum, frame):
        raise KeyboardInterrupt

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, handler)
    return previous


def _stop_child(child) -> None:
    if child is None:
        return
    try:
        child.terminate()
        child.wait(5)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.kill()
        child.wait()
    except OSError:
        pass


def _lock_call(path: str, body: dict) -> int:
    # при недоступной доске (рестарт) ждём и повторяем: очередь хранится на диске
    warned = False
    while True:
        try:
            return _request("POST", path, body)["position"]
        except CliError as e:
            if e.code != 2:
                raise
            if not warned:
                print("доска недоступна, жду…", file=sys.stderr, flush=True)
                warned = True
            time.sleep(_lock_interval())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_board")
    parser.add_argument("--project", help="slug проекта (по умолчанию $AGENT_BOARD_PROJECT)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(func=func)
        return sp

    sp = add("serve", cmd_serve, help="запустить доску")
    sp.add_argument("--port", type=int, default=None, help="по умолчанию постоянный 8737–8746")
    sp.add_argument("--no-runner", action="store_true", help="не запускать агентов (отладка доски)")
    sp.add_argument("--no-tracker-sync", action="store_true", help="не запускать фоновую синхронизацию трекера")
    sp.add_argument("--detach", action="store_true", help="запустить в фоне (лог в <home>/server.log), напечатать ссылку")
    add("stop", cmd_stop, help="остановить запущенную доску")
    add("version", cmd_version, help="версия agent-board")
    sp = add("init", cmd_init, help="создать проект")
    sp.add_argument("slug")
    sp.add_argument("--title", required=True)
    add("state", cmd_state, help="весь snapshot проекта")
    add("show", cmd_show).add_argument("key")
    sp = add("status", cmd_status, help="без аргументов: состояние доски и проектов; status <KEY> <STATUS>: "
                                         "перевести задачу")
    sp.add_argument("key", nargs="?")
    sp.add_argument("status", nargs="?")
    sp.add_argument("--json", action="store_true", help="состояние доски JSON-объектом")
    sp = add("update", cmd_update)
    sp.add_argument("key")
    for _, attr, _ in UPDATE_OPTIONS:
        sp.add_argument("--" + attr.replace("_", "-"), dest=attr)
    sp = add("ask", cmd_ask)
    sp.add_argument("key")
    sp.add_argument("--text", required=True)
    sp.add_argument("--option", action="append")
    sp.add_argument("--context")
    sp.add_argument("--recommended")
    sp = add("say", cmd_say)
    sp.add_argument("thread")
    sp.add_argument("--text", required=True)
    add("thread", cmd_thread).add_argument("thread")
    add("add-task", cmd_add_task).add_argument("--json", required=True)
    sp = add("deps", cmd_deps, help="заменить зависимости задачи списком ключей (только пользователь)")
    sp.add_argument("key")
    sp.add_argument("--json", required=True, help="ключи зависимостей JSON-списком")
    add("stage", cmd_stage, help="перевести проект на стадию: tracker, work, done (только пользователь)").add_argument(
        "stage")
    add("ship", cmd_ship, help="отправить одобренные задачи в ревью командой проекта (только пользователь)").add_argument(
        "keys", nargs="+")
    add("tracker", cmd_tracker, help="завести задачи в трекере (create), проверить доступ (check) или "
                                     "синхронизировать статусы сейчас (sync); только пользователь").add_argument(
        "action", choices=["create", "check", "sync"])
    sp = add("config", cmd_config, help="настройки проекта: показать, изменить или проверить (только пользователь)")
    actions = sp.add_subparsers(dest="action", required=True)
    actions.add_parser("get", help="показать настройки").add_argument("--json", action="store_true",
                                                                       help="вывести JSON-объектом")
    sp_set = actions.add_parser("set", help="изменить поля настроек (все проверки сохранения)")
    sp_set.add_argument("--json", dest="fields_json", required=True, help="поля настроек JSON-объектом")
    sp_set.add_argument("--dry-run", action="store_true", help="только проверить, не сохраняя")
    actions.add_parser("validate", help="проверить сохранённые настройки")
    actions.add_parser("schema", help="допустимые поля настроек: типы, значения по умолчанию, перечисления; "
                                      "доска не нужна").add_argument("--json", action="store_true",
                                                                     help="вывести JSON-объектом")
    sp = add("propose", cmd_propose)
    sp.add_argument("kind")
    sp.add_argument("--json", required=True)
    sp.add_argument("--reason")
    sp = add("lock-run", cmd_lock_run, help="выполнить команду под эксклюзивным ресурсом")
    sp.add_argument("resource")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    add("events", cmd_events).add_argument("--since", type=int, default=0)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except ExitWith as e:
        return e.code
    except CliError as e:
        print(str(e), file=sys.stderr)
        return e.code
    except FileExistsError:
        print("Проект уже существует: %s" % getattr(args, "slug", ""), file=sys.stderr)
        return 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
