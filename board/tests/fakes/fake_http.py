"""Основа поддельных трекеров: HTTP-сервер на localhost в отдельном потоке, который записывает запросы.

Сбои настраиваются по номеру запроса (`fail_on[n] = (status, body)`, счёт с 1) или для всех запросов
(`fail_all = (status, body)`); тело — объект, он отдаётся как JSON.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple


class Recorded:
    def __init__(self, method: str, path: str, headers: Dict[str, str], body):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    def __repr__(self):
        return "Recorded(%s %s %r)" % (self.method, self.path, self.body)


class FakeServer:
    def __init__(self):
        self.requests: List[Recorded] = []
        self.fail_on: Dict[int, Tuple[int, object]] = {}
        self.fail_all: Optional[Tuple[int, object]] = None
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._httpd.server_address[1]

    def start(self) -> "FakeServer":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                status, payload = fake._handle(self.command, self.path, dict(self.headers), body)
                data = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _any

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._thread.join(timeout=5)
            self._httpd = None

    def _handle(self, method: str, path: str, headers: Dict[str, str], body):
        with self._lock:
            self.requests.append(Recorded(method, path, headers, body))
            n = len(self.requests)
            if n in self.fail_on:
                return self.fail_on[n]
            if self.fail_all is not None:
                return self.fail_all
            if not self.authorized(headers):
                return 401, None
            return self.route(method, path, body)

    def authorized(self, headers: Dict[str, str]) -> bool:
        return True

    def route(self, method: str, path: str, body):
        raise NotImplementedError

    def calls(self, method: str, prefix: str) -> List[Recorded]:
        return [r for r in self.requests if r.method == method and r.path.startswith(prefix)]
