from __future__ import annotations

import errno
import fcntl
import hmac
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .board import Board, new_task_from_dict
from .model import Event, Question, Status, Task
from .resources import ResourceQueue
from .store import ProjectStore, atomic_write_json, board_home, list_projects
from .worktree import GitError, changed_files, diff as worktree_diff, worktree_path

COOKIE_NAME = "board_token"
COOKIE_MAX_AGE = 30 * 24 * 3600
KEEPALIVE_SECONDS = 15
MAX_BODY = 4 * 1024 * 1024
MAX_DIFF_BYTES = 400 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MESSAGE_AUTHORS = {"user", "agent", "project_agent"}
# постоянный порт, чтобы ссылка на доску переживала рестарт
DEFAULT_PORTS = list(range(8737, 8747))

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_CSP = ("default-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def discovery_path() -> Path:
    return board_home() / "server.json"


class ServerAlreadyRunning(RuntimeError):
    pass


def _port_open(port) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _load_or_create_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        os.chmod(str(path), 0o600)
        return token
    token = secrets.token_urlsafe(24)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return token


def read_discovery() -> Optional[dict]:
    """Параметры запущенного сервера или None, если файла нет или процесс мёртв."""
    try:
        data = json.loads(discovery_path().read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data


class _Hub:
    """Борды проектов (по одному на проект), подписки SSE и подписчики на события всех проектов."""

    def __init__(self):
        self._lock = threading.Lock()
        self._boards: Dict[str, Board] = {}
        self._conds: Dict[str, threading.Condition] = {}
        self._queues: Dict[str, ResourceQueue] = {}
        self._versions: Dict[str, int] = {}
        self._subscribers: List[Callable[[str, Event], None]] = []
        self.stopping = False

    def subscribe(self, callback: Callable[[str, Event], None]) -> None:
        """callback(slug, event) вызывается под блокировкой борда, по порядку seq; он не должен блокироваться."""
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Event], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def cond(self, slug: str) -> threading.Condition:
        with self._lock:
            return self._conds.setdefault(slug, threading.Condition())

    def version(self, slug: str) -> int:
        return self._versions.get(slug, 0)

    def board(self, slug: str) -> Board:
        with self._lock:
            board = self._boards.get(slug)
            if board is not None:
                return board
        try:
            store = ProjectStore(slug)
        except ValueError:
            raise KeyError("Неизвестный проект: %s" % slug)
        if not (store.root / "project.json").is_file():
            raise KeyError("Неизвестный проект: %s" % slug)
        with self._lock:
            if slug not in self._boards:
                board = Board(store, on_event=lambda e, s=slug: self._notify(s, e))
                board.locks_for = lambda key, s=slug: self._locks_of(s, key)
                self._boards[slug] = board
            return self._boards[slug]

    def _locks_of(self, slug: str, key: str) -> List[dict]:
        # очередь создаётся первым обращением lock-run; до него показывать нечего
        with self._lock:
            queue = self._queues.get(slug)
        return queue.locks_of(key) if queue is not None else []

    def queue(self, slug: str) -> ResourceQueue:
        board = self.board(slug)
        with self._lock:
            if slug not in self._queues:
                self._queues[slug] = ResourceQueue(board)
            return self._queues[slug]

    def _notify(self, slug: str, event: Event) -> None:
        cond = self.cond(slug)
        with cond:
            self._versions[slug] = self._versions.get(slug, 0) + 1
            cond.notify_all()
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(slug, event)
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def wake_all(self) -> None:
        self.stopping = True
        with self._lock:
            conds = list(self._conds.values())
        for cond in conds:
            with cond:
                cond.notify_all()


def _need(body: dict, name: str):
    if name not in body:
        raise ValueError("Не указано поле: %s" % name)
    return body[name]


def _event_ids(events: List[Event], type: str, key: str) -> Optional[str]:
    for e in events:
        if e.type == type:
            return e.data.get(key)
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard"
    protocol_version = "HTTP/1.0"

    # --- инфраструктура ---

    def log_message(self, format, *args):  # noqa: A002 - тишина в stderr
        pass

    @property
    def app(self) -> "BoardServer":
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _send_json(self, status: int, obj, extra_headers: Optional[List[Tuple[str, str]]] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _send_bytes(self, status, body: bytes, ctype: str, extra_headers=None, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _allowed_hosts(self) -> set:
        port = self.app.port
        return {"127.0.0.1:%d" % port, "localhost:%d" % port}

    def _token_ok(self, supplied: Optional[str]) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), self.app.token.encode("utf-8"))

    def _request_token(self) -> Optional[str]:
        header = self.headers.get("X-Board-Token")
        if header:
            return header
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return None

    def _dispatch(self, method: str) -> None:
        try:
            if self.headers.get("Host") not in self._allowed_hosts():
                return self._send_json(403, {"error": "forbidden host"})
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            path = url.path
            if path.startswith("/api/"):
                if not self._token_ok(self._request_token()):
                    return self._send_json(401, {"error": "unauthorized"})
                if method == "POST":
                    origin = self.headers.get("Origin")
                    if origin is not None and origin not in {"http://" + h for h in self._allowed_hosts()}:
                        return self._send_json(403, {"error": "forbidden origin"})
                return self._api(method, path, query)
            if method != "GET":
                return self._send_json(405, {"error": "method not allowed"})
            return self._static(path, query)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except PermissionError as e:
            self._send_json(403, {"error": str(e)})
        except KeyError as e:
            self._send_json(404, {"error": str(e.args[0]) if e.args else "not found"})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._send_json(500, {"error": "internal"})
            except OSError:
                pass

    # --- статика ---

    def _static(self, path: str, query: dict) -> None:
        """`/` — страница приложения, `/static/...` — его файлы; всё прочее — 404."""
        headers: List[Tuple[str, str]] = []
        if path == "/":
            if self._token_ok((query.get("token") or [None])[0]):
                headers.append(("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
                    COOKIE_NAME, self.app.token, COOKIE_MAX_AGE)))
            rel = "index.html"
        elif path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
        else:
            raise KeyError("not found")
        root = Path(self.app.static_dir or STATIC_DIR).resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            raise KeyError("not found")
        ctype = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        headers += [("Content-Security-Policy", APP_CSP), ("X-Content-Type-Options", "nosniff")]
        self._send_bytes(200, target.read_bytes(), ctype, headers, cache="no-cache")

    # --- API ---

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Некорректный Content-Length")
        if length < 0 or length > MAX_BODY:
            raise ValueError("Слишком большое тело запроса")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Некорректный JSON")
        if not isinstance(body, dict):
            raise ValueError("Тело запроса должно быть JSON-объектом")
        return body

    def _api(self, method: str, path: str, query: dict) -> None:
        parts = [unquote(p) for p in path.split("/")[2:]]
        if method == "GET" and parts == ["projects"]:
            return self._send_json(200, {"projects": list_projects()})
        if len(parts) < 3 or parts[0] != "p":
            raise KeyError("Неизвестный маршрут")
        board = self.app.hub.board(parts[1])
        rest = parts[2:]
        if method == "POST" and len(rest) == 3 and rest[0] == "locks":
            return self._api_lock(parts[1], rest[1], rest[2])
        if method == "GET":
            return self._api_get(board, parts[1], rest, query)
        return self._api_post(board, rest)

    def _api_get(self, board: Board, slug: str, rest: List[str], query: dict) -> None:
        if rest == ["state"]:
            return self._send_json(200, board.snapshot())
        if len(rest) == 2 and rest[0] == "threads":
            messages = board.store.read_thread(rest[1])
            return self._send_json(200, {"messages": [m.to_dict() for m in messages]})
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "diff":
            return self._send_json(200, self._task_diff(board, rest[1]))
        if rest == ["file"]:
            return self._send_file(board, query)
        if rest == ["events"]:
            events = board.store.read_events(self._since(query))
            return self._send_json(200, {"events": [e.to_dict() for e in events]})
        if rest == ["stream"]:
            return self._stream(board, slug, query)
        raise KeyError("Неизвестный маршрут")

    @staticmethod
    def _repo_path(config, task: Task) -> Optional[Path]:
        repos = [r for r in config.repos if isinstance(r, dict) and r.get("path")]
        if task.repo:
            repo = next((r for r in repos if r.get("name") == task.repo), None)
        else:
            repo = repos[0] if len(repos) == 1 else None
        return Path(repo["path"]) if repo else None

    def _task_diff(self, board: Board, key: str) -> dict:
        """Изменения задачи относительно её базовой ветки; git запускается только в собственном worktree задачи."""
        empty = {"base": None, "files": [], "diff": "", "truncated": False}
        task = board.store.load_tasks().get(key)
        if task is None:
            raise KeyError("Неизвестная задача: %s" % key)
        if not task.worktree or not task.base_branch:
            return empty
        repo = self._repo_path(board.store.load_config(), task)
        if repo is None or Path(task.worktree).resolve() != worktree_path(repo, key).resolve():
            return empty
        path = Path(task.worktree)
        if not path.is_dir():
            return empty
        try:
            files = changed_files(path, task.base_branch)
            text = worktree_diff(path, task.base_branch)
        except GitError as e:
            raise ValueError("Не удалось получить изменения: %s" % e)
        raw = text.encode("utf-8")
        truncated = len(raw) > MAX_DIFF_BYTES
        if truncated:
            raw = raw[:MAX_DIFF_BYTES]
            # режем по границе строки, чтобы не оставить обрывок; если перевода строки нет — по байтам
            raw = raw[:raw.rfind(b"\n") + 1] or raw
            text = raw.decode("utf-8", "ignore")
        return {"base": task.base_branch, "files": files, "diff": text, "truncated": truncated}

    def _send_file(self, board: Board, query: dict) -> None:
        """Отдаёт локальный файл, если он лежит в каталоге проекта, его репозиториев или worktree задач.

        Относительный путь считается от первого репозитория проекта, а без репозиториев — от каталога проекта.
        """
        raw = (query.get("path") or [""])[0]
        if not raw:
            raise ValueError("Не указан path")
        root = board.store.root.resolve()
        config = board.store.load_config()
        repos = [Path(r["path"]).resolve() for r in config.repos if isinstance(r, dict) and r.get("path")]
        target = ((repos[0] if repos else root) / raw).resolve()
        allowed = [root] + repos
        base = next((b for b in allowed if target == b or b in target.parents), None)
        if base is None:
            raise PermissionError("Путь вне каталогов проекта")
        rel = [part.lower() for part in target.relative_to(base).parts]
        # внутренности git и логи агентов не отдаются; регистр не важен — ФС macOS к нему нечувствительна
        if ".git" in rel or (base == root and rel[:1] == ["logs"]):
            raise PermissionError("Путь недоступен")
        if not target.is_file():
            raise KeyError("Файл не найден")
        if target.stat().st_size > MAX_FILE_BYTES:
            return self._send_json(413, {"error": "Файл больше %d МБ" % (MAX_FILE_BYTES // (1024 * 1024))})
        body = target.read_bytes()
        if target.suffix.lower() in (".html", ".htm"):
            return self._send_bytes(200, body, "text/html; charset=utf-8", [
                ("Content-Security-Policy", "sandbox"), ("X-Content-Type-Options", "nosniff")])
        self._send_bytes(200, body, "text/plain; charset=utf-8", [("X-Content-Type-Options", "nosniff")])

    def _since(self, query: dict) -> int:
        # браузер при переподключении шлёт Last-Event-ID, а since остаётся из исходного URL
        result = 0
        for raw in (query.get("since") or []) + [self.headers.get("Last-Event-ID") or "0"]:
            try:
                result = max(result, int(raw))
            except ValueError:
                raise ValueError("Некорректный since: %r" % raw)
        return result

    def _api_lock(self, slug: str, resource: str, action: str) -> None:
        body = self._read_body()
        task, pid = _need(body, "task"), _need(body, "pid")
        if not isinstance(task, str) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("task должен быть строкой, pid — положительным числом")
        queue = self.app.hub.queue(slug)
        if action == "release":
            queue.release(resource, task, pid)
            return self._send_json(200, {"position": None})
        if action not in ("acquire", "poll"):
            raise KeyError("Неизвестный маршрут")
        position = getattr(queue, action)(resource, task, pid)
        self._send_json(200, {"position": position})

    def _api_post(self, board: Board, rest: List[str]) -> None:
        body = self._read_body()
        extra: dict = {}
        if len(rest) == 3 and rest[0] == "threads" and rest[2] == "read":
            events = board.mark_read(rest[1])
        elif len(rest) == 3 and rest[0] == "threads" and rest[2] == "messages":
            author = _need(body, "author")
            if author not in MESSAGE_AUTHORS:
                raise ValueError("Недопустимый автор: %r" % (author,))
            from_task = body.get("from_task")
            if from_task is not None and not isinstance(from_task, str):
                raise ValueError("from_task должен быть строкой")
            events = board.post_message(rest[1], author, str(_need(body, "text")), from_task=from_task or None)
        elif rest == ["stage"]:
            events = board.set_stage(_need(body, "stage"), _need(body, "actor"))
        elif rest == ["tasks"]:
            task = self._task(_need(body, "task"))
            events = board.add_task(task, _need(body, "actor"))
            extra["key"] = task.key
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "transition":
            events = board.transition(rest[1], Status(_need(body, "to")), _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "review":
            events = board.request_review(rest[1], _need(body, "actor"))
        elif rest == ["ship"]:
            events = board.request_ship(_need(body, "keys"), _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "approve":
            events = board.approve(rest[1], _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "return":
            events = board.return_to_work(rest[1], _need(body, "actor"), _need(body, "text"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "update":
            events = board.update_task(rest[1], _need(body, "actor"), _need(body, "fields"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "deps":
            deps = _need(body, "deps")
            if not isinstance(deps, list):
                raise ValueError("deps должен быть списком")
            events = board.set_deps(rest[1], deps, _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "questions":
            events = board.ask(rest[1], self._question(_need(body, "question")))
            extra["question_id"] = _event_ids(events, "question_asked", "question_id")
        elif len(rest) == 5 and rest[0] == "tasks" and rest[2] == "questions" and rest[4] == "answer":
            events = board.answer(rest[1], rest[3], body.get("choice"), str(body.get("comment") or ""))
        elif rest == ["proposals"]:
            payload = _need(body, "payload")
            if not isinstance(payload, dict):
                raise ValueError("payload должен быть объектом")
            events = board.propose(_need(body, "kind"), payload, str(body.get("reason") or ""))
            extra["proposal_id"] = _event_ids(events, "proposal_created", "id")
        elif len(rest) == 3 and rest[0] == "proposals" and rest[2] == "apply":
            events = board.apply_proposal(rest[1])
        elif len(rest) == 3 and rest[0] == "proposals" and rest[2] == "reject":
            events = board.reject_proposal(rest[1])
        else:
            raise KeyError("Неизвестный маршрут")
        payload = {"events": [e.to_dict() for e in events]}
        payload.update(extra)
        self._send_json(200, payload)

    @staticmethod
    def _task(d) -> Task:
        return new_task_from_dict(d)

    @staticmethod
    def _question(d) -> Question:
        if not isinstance(d, dict):
            raise ValueError("question должен быть объектом")
        try:
            return Question.from_dict(dict({"id": ""}, **d))
        except TypeError as e:
            raise ValueError("Некорректный вопрос: %s" % e)

    # --- SSE ---

    def _stream(self, board: Board, slug: str, query: dict) -> None:
        last = self._since(query)
        hub = self.app.hub
        cond = hub.cond(slug)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        try:
            while not hub.stopping:
                # версия фиксируется до чтения, чтобы не пропустить событие между чтением и ожиданием
                with cond:
                    version = hub.version(slug)
                chunk = ""
                for e in board.store.read_events(last):
                    chunk += "id: %d\nevent: %s\ndata: %s\n\n" % (e.seq, e.type, json.dumps(e.to_dict(), ensure_ascii=False))
                    last = e.seq
                if chunk:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                with cond:
                    if hub.version(slug) == version and not hub.stopping:
                        if not cond.wait(KEEPALIVE_SECONDS) and not hub.stopping:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class BoardServer:
    """Сервер доски. port=None — постоянный порт из DEFAULT_PORTS (или случайный, если все заняты)."""

    def __init__(self, port: Optional[int] = None, static_dir: Optional[Path] = None, runner: bool = False):
        self.port = port
        self._requested_port = port
        self.static_dir = static_dir
        self.token: Optional[str] = None
        self.hub = _Hub()
        self._httpd: Optional[_HttpServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock_fd: Optional[int] = None
        self._with_runner = runner
        self.runner = None

    def board_for(self, slug: str) -> Board:
        return self.hub.board(slug)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/?token=%s" % (self.port, self.token)

    def start(self) -> None:
        home = board_home()
        home.mkdir(parents=True, exist_ok=True)
        self._acquire_lock(home / "server.lock")
        try:
            running = read_discovery()
            if running is not None and _port_open(running.get("port")):
                raise ServerAlreadyRunning(
                    "Доска уже запущена (pid %s, порт %s)" % (running.get("pid"), running.get("port")))
            self.token = _load_or_create_token(home / "token")
            httpd = self._bind()
        except BaseException:
            self._release_lock()
            raise
        try:
            httpd.app = self  # type: ignore[attr-defined]
            self._httpd = httpd
            self.port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, name="board-http", daemon=True)
            thread.start()
            self._thread = thread
            path = discovery_path()
            atomic_write_json(path, {"port": self.port, "token": self.token, "pid": os.getpid()})
            os.chmod(str(path), 0o600)
            if self._with_runner:
                from .runner import Runner

                runner = Runner(self)
                runner.start()
                self.runner = runner
        except BaseException:
            self._rollback_start(httpd)
            raise

    def _rollback_start(self, httpd: _HttpServer) -> None:
        if self._thread is not None:
            httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        httpd.server_close()
        self._httpd = None
        self._remove_own_discovery()
        self.port = self._requested_port
        self._release_lock()

    def _bind(self) -> _HttpServer:
        if self.port is not None:
            return _HttpServer(("127.0.0.1", self.port), _Handler)
        for port in DEFAULT_PORTS:
            # на macOS bind на 127.0.0.1 проходит даже при чужом слушателе на 0.0.0.0
            if _port_open(port):
                continue
            try:
                return _HttpServer(("127.0.0.1", port), _Handler)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
        return _HttpServer(("127.0.0.1", 0), _Handler)

    def _acquire_lock(self, path: Path) -> None:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ServerAlreadyRunning("Доска уже запущена с этим каталогом: %s" % path.parent)
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self.runner = None
        self.hub.wake_all()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._lock_fd is not None:
            self._remove_own_discovery()
        self._release_lock()

    def _remove_own_discovery(self) -> None:
        path = discovery_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("pid") == os.getpid() and data.get("token") == self.token:
            path.unlink()
