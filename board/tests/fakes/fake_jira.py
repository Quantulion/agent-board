"""Поддельная Jira (REST v2): заведение задач, связи, поиск статусов, проверка доступа.

`auth` — ожидаемое значение заголовка Authorization или Cookie (None — без проверки).
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

    def authorized(self, headers) -> bool:
        if self.authorization is not None and headers.get("Authorization") != self.authorization:
            return False
        if self.cookie is not None and headers.get("Cookie") != self.cookie:
            return False
        return True

    def route(self, method, path, body):
        url = urlsplit(path)
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
        if method == "GET" and url.path == "/rest/api/2/search":
            jql = parse_qs(url.query).get("jql", [""])[0]
            keys = re.findall(r"[A-Z]+-\d+", jql)
            return 200, {"issues": [{"key": k, "fields": {"status": {"name": self.statuses[k]}}}
                                    for k in keys if k in self.statuses]}
        return 404, {"errorMessages": ["not found"]}
