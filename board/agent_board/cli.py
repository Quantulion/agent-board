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
from .messages import LANG_HEADER, current_language, msg
from .server import BoardServer, ServerAlreadyRunning, read_discovery
from .store import ProjectStore

LOCK_POLL_SECONDS = 2.0


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
        raise CliError(msg("cli.no_project"))
    return slug


def _json_arg(raw: str, name: str):
    try:
        return json.loads(raw)
    except ValueError as e:
        raise CliError(msg("cli.bad_json", name=name, error=str(e)))


def _request(method: str, path: str, body: Optional[dict] = None, timeout: float = 10):
    info = read_discovery()
    if info is None:
        raise CliError(msg("cli.not_running"), 2)
    headers = {"X-Board-Token": info["token"], LANG_HEADER: current_language()}
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
        raise CliError(msg("cli.not_running"), 2)
    except socket.timeout:
        # соединение есть, ответа нет: сервер мог не закончить запрос
        raise CliError(msg("cli.timeout", seconds=timeout), 2)


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
    raise CliError(msg("cli.unknown_task", key=args.key))


def cmd_state(args):
    return _request("GET", _p(args, "state"))


def cmd_version(args):
    print(__version__)
    return None


def cmd_self_update(args):
    from . import install

    code = install.run(args.repo, args.ref)
    if code:
        raise ExitWith(code)
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
        raise CliError(msg("cli.status_usage"))
    return _request("POST", _p(args, "tasks/%s/transition" % args.key), {"to": args.status, "actor": _actor()})


UPDATE_OPTIONS = [
    ("summary", "summary", False),
    ("todo", "todo_json", True),
    ("changed_files", "changed_files_json", True),
    ("report", "report", False),
    ("rule_violations", "rule_violations_json", True),
    ("title", "title", False),
    ("description", "description", False),
    ("done_when", "done_when", False),
    ("estimate", "estimate", True),
    ("tracker_key", "tracker_key", False),
    ("tracker_url", "tracker_url", False),
]

# пустое значение снимает привязку задачи к трекеру
CLEARABLE_FIELDS = {"tracker_key", "tracker_url"}


def cmd_update(args):
    fields = {}
    for field, attr, is_json in UPDATE_OPTIONS:
        value = getattr(args, attr)
        if value is None:
            continue
        if field in CLEARABLE_FIELDS and value == "":
            fields[field] = None
        else:
            fields[field] = _json_arg(value, "--" + attr.replace("_", "-")) if is_json else value
    return _request("POST", _p(args, "tasks/%s/update" % args.key), {"actor": _user_actor(), "fields": fields})


def cmd_ask(args):
    question = {"text": args.text, "options": args.option or [], "context": args.context or ""}
    if args.recommended is not None:
        question["recommended"] = args.recommended
    return _request("POST", _p(args, "tasks/%s/questions" % args.key), {"question": question})


def cmd_say(args):
    body = {"author": _user_actor(), "text": args.text}
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


def cmd_retry(args):
    return _request("POST", _p(args, "tasks/%s/retry" % args.key), {"actor": _user_actor()})


def cmd_rebase(args):
    return _request("POST", _p(args, "tasks/%s/rebase" % args.key), {"actor": _user_actor()})


def cmd_ship(args):
    return _request("POST", _p(args, "ship"), {"keys": args.keys, "actor": _user_actor()})


# заведение задач идёт синхронно: по запросу к трекеру на задачу и связь
TRACKER_TIMEOUT = 600


def cmd_tracker(args):
    result = _request("POST", _p(args, "tracker/" + args.action), {"actor": _user_actor()},
                      timeout=TRACKER_TIMEOUT)
    problems = [e["error"] for e in result.get("errors") or []]
    if args.action in ("check", "sync") and not result.get("ok"):
        problems.append(result.get("error") or msg("cli.tracker_unavailable"))
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
        raise CliError(msg("cli.no_command"))
    task = os.environ.get("AGENT_BOARD_TASK")
    if not task:
        raise CliError(msg("cli.no_task_env"))
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
                print(msg("cli.lock_waiting", resource=args.resource, position=position), file=sys.stderr, flush=True)
                shown = position
            time.sleep(interval)
            position = _lock_call(base + "poll", body)
        try:
            child = subprocess.Popen(cmd)
        except OSError as e:
            print(msg("cli.run_failed", command=cmd[0], error=e), file=sys.stderr)
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
                print(msg("cli.release_failed", resource=args.resource, error=e), file=sys.stderr)


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
                print(msg("cli.board_unavailable"), file=sys.stderr, flush=True)
                warned = True
            time.sleep(_lock_interval())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_board")
    parser.add_argument("--project", help=msg("help.project"))
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, **kw):
        sp = sub.add_parser(name, **kw)
        sp.set_defaults(func=func)
        return sp

    sp = add("serve", cmd_serve, help=msg("help.serve"))
    sp.add_argument("--port", type=int, default=None, help=msg("help.port"))
    sp.add_argument("--no-runner", action="store_true", help=msg("help.no_runner"))
    sp.add_argument("--no-tracker-sync", action="store_true", help=msg("help.no_tracker_sync"))
    sp.add_argument("--detach", action="store_true", help=msg("help.detach"))
    add("stop", cmd_stop, help=msg("help.stop"))
    add("version", cmd_version, help=msg("help.version"))
    sp = add("self-update", cmd_self_update, help=msg("help.self_update"))
    sp.add_argument("--ref", help=msg("help.ref"))
    sp.add_argument("--repo", help=msg("help.repo"))
    sp = add("init", cmd_init, help=msg("help.init"))
    sp.add_argument("slug")
    sp.add_argument("--title", required=True)
    add("state", cmd_state, help=msg("help.state"))
    add("show", cmd_show, help=msg("help.show")).add_argument("key")
    sp = add("status", cmd_status, help=msg("help.status"))
    sp.add_argument("key", nargs="?")
    sp.add_argument("status", nargs="?")
    sp.add_argument("--json", action="store_true", help=msg("help.status_json"))
    sp = add("update", cmd_update, help=msg("help.update"))
    sp.add_argument("key")
    for _, attr, _ in UPDATE_OPTIONS:
        sp.add_argument("--" + attr.replace("_", "-"), dest=attr)
    sp = add("ask", cmd_ask, help=msg("help.ask"))
    sp.add_argument("key")
    sp.add_argument("--text", required=True)
    sp.add_argument("--option", action="append")
    sp.add_argument("--context")
    sp.add_argument("--recommended")
    sp = add("say", cmd_say, help=msg("help.say"))
    sp.add_argument("thread")
    sp.add_argument("--text", required=True)
    add("thread", cmd_thread, help=msg("help.thread")).add_argument("thread")
    add("add-task", cmd_add_task, help=msg("help.add_task")).add_argument("--json", required=True)
    sp = add("deps", cmd_deps, help=msg("help.deps"))
    sp.add_argument("key")
    sp.add_argument("--json", required=True, help=msg("help.deps_json"))
    add("stage", cmd_stage, help=msg("help.stage")).add_argument(
        "stage")
    add("ship", cmd_ship, help=msg("help.ship")).add_argument(
        "keys", nargs="+")
    add("retry", cmd_retry, help=msg("help.retry")).add_argument("key")
    add("rebase", cmd_rebase, help=msg("help.rebase")).add_argument("key")
    add("tracker", cmd_tracker, help=msg("help.tracker")).add_argument(
        "action", choices=["create", "check", "sync"])
    sp = add("config", cmd_config, help=msg("help.config"))
    actions = sp.add_subparsers(dest="action", required=True)
    actions.add_parser("get", help=msg("help.config_get")).add_argument("--json", action="store_true",
                                                                       help=msg("help.as_json"))
    sp_set = actions.add_parser("set", help=msg("help.config_set"))
    sp_set.add_argument("--json", dest="fields_json", required=True, help=msg("help.config_fields"))
    sp_set.add_argument("--dry-run", action="store_true", help=msg("help.dry_run"))
    actions.add_parser("validate", help=msg("help.config_validate"))
    actions.add_parser("schema", help=msg("help.config_schema")).add_argument("--json", action="store_true",
                                                                     help=msg("help.as_json"))
    sp = add("propose", cmd_propose, help=msg("help.propose"))
    sp.add_argument("kind")
    sp.add_argument("--json", required=True)
    sp.add_argument("--reason")
    sp = add("lock-run", cmd_lock_run, help=msg("help.lock_run"))
    sp.add_argument("resource")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    add("events", cmd_events, help=msg("help.events")).add_argument("--since", type=int, default=0)
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
        print(msg("cli.project_exists", slug=getattr(args, "slug", "")), file=sys.stderr)
        return 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
