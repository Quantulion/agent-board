"""Поддельный GitHub Issues API: заведение задач, комментарии, чтение задач, проверка доступа к репозиторию."""
from __future__ import annotations

import re
from typing import Dict, Optional

from .fake_http import FakeServer


class FakeGitHub(FakeServer):
    def __init__(self, repo: str = "owner/name", token: Optional[str] = None):
        super().__init__()
        self.repo = repo
        self.token = token
        self.issues: Dict[int, dict] = {}
        self.comments: Dict[int, list] = {}

    def authorized(self, headers) -> bool:
        return self.token is None or headers.get("Authorization") == "Bearer " + self.token

    def route(self, method, path, body):
        base = "/repos/" + self.repo
        if method == "GET" and path == base:
            return 200, {"full_name": self.repo}
        if method == "POST" and path == base + "/issues":
            number = len(self.issues) + 1
            self.issues[number] = dict(body, state="open")
            return 201, {"number": number, "html_url": "https://github.example/%s/issues/%d" % (self.repo, number)}
        m = re.fullmatch(re.escape(base) + r"/issues/(\d+)(/comments)?", path)
        if m and int(m.group(1)) in self.issues:
            number = int(m.group(1))
            if method == "POST" and m.group(2):
                self.comments.setdefault(number, []).append(body["body"])
                return 201, {"id": 1}
            if method == "GET" and not m.group(2):
                return 200, {"number": number, "state": self.issues[number]["state"]}
        return 404, {"message": "Not Found"}
