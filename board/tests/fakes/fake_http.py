"""Основа поддельных трекеров: HTTP-сервер на localhost в отдельном потоке, который записывает запросы.

Сбои настраиваются по номеру запроса (`fail_on[n] = (status, body)`, счёт с 1) или для всех запросов
(`fail_all = (status, body)`); тело — объект, он отдаётся как JSON; третьим элементом можно передать заголовки
ответа. Вместо ответа можно задать HANG (ответа нет HANG_SECONDS, потом соединение закрывается) или TRUNCATE
(тело короче заявленного Content-Length).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional


HANG = "hang"
TRUNCATE = "truncate"
HANG_SECONDS = 2.0


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
        self.fail_on: Dict[int, object] = {}
        self.fail_all: Optional[object] = None
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
                result = fake._handle(self.command, self.path, dict(self.headers), body)
                if result == HANG:
                    time.sleep(HANG_SECONDS)
                    return
                if result == TRUNCATE:
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b'{"key"')
                    return
                status, payload = result[0], result[1]
                data = b"" if payload is None else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (result[2] if len(result) > 2 else {}).items():
                    self.send_header(name, value)
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
