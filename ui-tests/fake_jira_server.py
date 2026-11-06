"""Поддельная Jira для ui-тестов: та же FakeJira, что в тестах сервера, отдельным процессом.

Печатает свой адрес первой строкой и работает, пока открыт stdin (закрытие stdin или SIGTERM завершают процесс).
Управление тестом: POST /_control/status {"key", "name"} меняет статус задачи, GET /_control/state — заведённое.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board" / "tests"))

from fakes.fake_jira import FakeJira  # noqa: E402


class ControlledJira(FakeJira):
    def route(self, method, path, body):
        if method == "POST" and path == "/_control/status":
            if body.get("key") not in self.statuses:
                return 404, {"error": "нет задачи %s" % body.get("key")}
            self.statuses[body["key"]] = body["name"]
            return 200, {"ok": True}
        if method == "GET" and path == "/_control/state":
            return 200, {"issues": self.issues, "links": self.links, "statuses": self.statuses}
        return super().route(method, path, body)


def main() -> None:
    jira = ControlledJira(project="PROJ").start()
    print(json.dumps({"url": jira.url}), flush=True)
    try:
        sys.stdin.read()
    finally:
        jira.stop()


if __name__ == "__main__":
    main()
