from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .board import Board
from .model import Event, Question, Status, Task
from .store import ProjectStore, atomic_write_json, board_home, list_projects

COOKIE_NAME = "board_token"
KEEPALIVE_SECONDS = 15
MAX_BODY = 4 * 1024 * 1024
MESSAGE_AUTHORS = {"user", "agent", "project_agent"}

STUB_PAGE = (
    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>Доска</title></head>"
    "<body><p>Доска запускается</p></body></html>"
)


def discovery_path() -> Path:
    return board_home() / "server.json"


def read_discovery() -> Optional[dict]:
    """Параметры запущенного сервера или None, если файла нет или процесс мёртв."""
    try:
        data = json.loads(discovery_path().read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data


class _Hub:
    """Борды проектов (по одному на проект) и подписки SSE на новые события."""

    def __init__(self):
        self._lock = threading.Lock()
        self._boards: Dict[str, Board] = {}
        self._conds: Dict[str, threading.Condition] = {}
        self._versions: Dict[str, int] = {}
        self.stopping = False

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
                self._boards[slug] = Board(store, on_event=lambda e, s=slug: self._notify(s))
            return self._boards[slug]

    def _notify(self, slug: str) -> None:
        cond = self.cond(slug)
        with cond:
            self._versions[slug] = self._versions.get(slug, 0) + 1
            cond.notify_all()

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

    def _send_bytes(self, status, body: bytes, ctype: str, extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        headers: List[Tuple[str, str]] = []
        if path == "/" and self._token_ok((query.get("token") or [None])[0]):
            headers.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE_NAME, self.app.token)))
        static_dir = self.app.static_dir
        if static_dir is None:
            if path != "/":
                return self._send_json(404, {"error": "not found"})
            return self._send_bytes(200, STUB_PAGE.encode("utf-8"), "text/html; charset=utf-8", headers)
        root = Path(static_dir).resolve()
        rel = unquote(path).lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            return self._send_json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._send_json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send_bytes(200, target.read_bytes(), ctype, headers)

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
        if method == "GET":
            return self._api_get(board, parts[1], rest, query)
        return self._api_post(board, rest)

    def _api_get(self, board: Board, slug: str, rest: List[str], query: dict) -> None:
        if rest == ["state"]:
            return self._send_json(200, board.snapshot())
        if len(rest) == 2 and rest[0] == "threads":
            messages = board.store.read_thread(rest[1])
            return self._send_json(200, {"messages": [m.to_dict() for m in messages]})
        if rest == ["events"]:
            events = board.store.read_events(self._since(query))
            return self._send_json(200, {"events": [e.to_dict() for e in events]})
        if rest == ["stream"]:
            return self._stream(board, slug, query)
        raise KeyError("Неизвестный маршрут")

    def _since(self, query: dict) -> int:
        raw = (query.get("since") or [self.headers.get("Last-Event-ID") or "0"])[0]
        try:
            return int(raw)
        except ValueError:
            raise ValueError("Некорректный since: %r" % raw)

    def _api_post(self, board: Board, rest: List[str]) -> None:
        body = self._read_body()
        extra: dict = {}
        if len(rest) == 3 and rest[0] == "threads" and rest[2] == "read":
            events = board.mark_read(rest[1])
        elif len(rest) == 3 and rest[0] == "threads" and rest[2] == "messages":
            author = _need(body, "author")
            if author not in MESSAGE_AUTHORS:
                raise ValueError("Недопустимый автор: %r" % (author,))
            events = board.post_message(rest[1], author, str(_need(body, "text")))
        elif rest == ["tasks"]:
            events = board.add_task(self._task(_need(body, "task")), _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "transition":
            events = board.transition(rest[1], Status(_need(body, "to")), _need(body, "actor"))
        elif len(rest) == 3 and rest[0] == "tasks" and rest[2] == "update":
            fields = _need(body, "fields")
            if not isinstance(fields, dict):
                raise ValueError("fields должен быть объектом")
            events = board.update_task(rest[1], _need(body, "actor"), **fields)
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
        if not isinstance(d, dict):
            raise ValueError("task должен быть объектом")
        try:
            return Task.from_dict(d)
        except TypeError as e:
            raise ValueError("Некорректная задача: %s" % e)

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
    def __init__(self, port: int = 0, static_dir: Optional[Path] = None):
        self.port = port
        self.static_dir = static_dir
        self.token = secrets.token_urlsafe(24)
        self.hub = _Hub()
        self._httpd: Optional[_HttpServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/?token=%s" % (self.port, self.token)

    def start(self) -> None:
        httpd = _HttpServer(("127.0.0.1", self.port), _Handler)
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, name="board-http", daemon=True)
        self._thread.start()
        home = board_home()
        home.mkdir(parents=True, exist_ok=True)
        path = discovery_path()
        atomic_write_json(path, {"port": self.port, "token": self.token, "pid": os.getpid()})
        os.chmod(str(path), 0o600)

    def stop(self) -> None:
        self.hub.wake_all()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            discovery_path().unlink()
        except FileNotFoundError:
            pass
