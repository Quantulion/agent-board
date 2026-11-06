"""Поддельная Jira (REST v2): заведение задач, связи, поиск статусов, проверка доступа.

`auth` — ожидаемое значение заголовка Authorization или Cookie (None — без проверки).
`anonymous` — как Jira с просроченной cookie: запросы проходят без авторизации от имени анонимного пользователя
("header" — поиск пуст, ответы с заголовком X-AUSERNAME: anonymous, /myself — 200; "silent" — поиск пуст без
заголовка, /myself — 401).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from .fake_http import FakeServer


class FakeJira(FakeServer):
    def __init__(self, project: str = "MNZ", authorization: Optional[str] = None, cookie: Optional[str] = None):
        super().__init__()
        self.project = project
        self.authorization = authorization
        self.cookie = cookie
        self.issues: Dict[str, dict] = {}
        self.links: List[dict] = []
        self.statuses: Dict[str, str] = {}
        # подмена ответа поиска (например, не объект)
        self.search_response = None
        # как Jira Server: неизвестный ключ в запросе без validateQuery=warn — 400
        self.strict_missing = True
        # отказ на запросы с несколькими ключами (проверка поключевого повтора)
        self.reject_batches = False
        self.anonymous: Optional[str] = None

    def authorized(self, headers) -> bool:
        if self.anonymous:
            return True
        if self.authorization is not None and headers.get("Authorization") != self.authorization:
            return False
        if self.cookie is not None and headers.get("Cookie") != self.cookie:
            return False
        return True

    def route(self, method, path, body):
        url = urlsplit(path)
        if self.anonymous:
            return self._anonymous(method, url)
        if method == "GET" and url.path == "/rest/api/2/myself":
            return 200, {"name": "tester"}
        m = re.fullmatch(r"/rest/api/2/project/([^/]+)", url.path)
        if method == "GET" and m:
            if m.group(1) != self.project:
                return 404, {"errorMessages": ["No project could be found with key '%s'." % m.group(1)]}
            return 200, {"key": self.project}
        if method == "POST" and url.path == "/rest/api/2/issue":
            key = "%s-%d" % (self.project, len(self.issues) + 1)
            self.issues[key] = body
            self.statuses[key] = "Open"
            return 201, {"id": str(len(self.issues)), "key": key, "self": "x"}
        if method == "POST" and url.path == "/rest/api/2/issueLink":
            self.links.append(body)
            return 201, None
        if method == "GET" and url.path in ("/rest/api/2/search", "/rest/api/2/search/jql"):
            if self.search_response is not None:
                return 200, self.search_response
            query = parse_qs(url.query)
            jql = query.get("jql", [""])[0]
            keys = re.findall(r"[A-Z]+-\d+", jql)
            if self.reject_batches and len(keys) > 1:
                return 400, {"errorMessages": ["batch rejected"]}
            if self.strict_missing and query.get("validateQuery") != ["warn"] and any(k not in self.statuses for k in keys):
                return 400, {"errorMessages": ["An issue with key '%s' does not exist for field 'key'." % keys[0]]}
            return 200, {"issues": [{"key": k, "fields": {"status": {"name": self.statuses[k]}}}
                                    for k in keys if k in self.statuses]}
        return 404, {"errorMessages": ["not found"]}

    def _anonymous(self, method, url):
        header = {"X-AUSERNAME": "anonymous"} if self.anonymous == "header" else {}
        if method == "GET" and url.path == "/rest/api/2/myself":
            if self.anonymous == "header":
                return 200, {}, header
            return 401, {"errorMessages": ["You are not authenticated."]}
        if method == "GET" and url.path in ("/rest/api/2/search", "/rest/api/2/search/jql"):
            return 200, {"issues": [], "warningMessages": ["The issue key does not exist."]}, header
        if method == "GET" and url.path.startswith("/rest/api/2/project/"):
            return 404, {"errorMessages": ["No project could be found."]}, header
        return 401, None
