"""Трекеры задач (Jira, GitHub Issues): заведение задач доски, связи зависимостей, чтение статусов.

Секреты (токены, cookie) берутся только из переменных окружения или файлов, указанных в настройках,
и не попадают ни в настройки, ни в журнал, ни в тексты ошибок: всё, что уходит наружу, проходит через scrub.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import string
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from .model import Status, Task
from .ship import order as dependency_order

TIMEOUT = 15.0
MAX_MESSAGE_CHARS = 500
# более короткие значения не маскируются, иначе текст превратится в звёздочки
MIN_SECRET_CHARS = 4
TITLE_PLACEHOLDERS = ("key", "title")
JIRA_SEARCH_BATCH = 50
TYPES = ("none", "jira", "github")
JIRA_KEYS = {"type", "url", "project", "issue_type", "epic", "epic_field", "components", "labels", "extra_fields",
             "link_type", "auth", "statuses", "title_template", "poll_seconds"}
GITHUB_KEYS = {"type", "repo", "labels", "auth", "statuses", "title_template", "api_url", "poll_seconds"}
GITHUB_API = "https://api.github.com"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class TrackerError(ValueError):
    """Ошибка трекера с текстом без секретов; kind: config, auth, not_found, validation, retryable, network, error."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class Secret:
    """Значение секрета, которое не печатается ни через str, ни через repr."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__


def scrub(text: str, secrets: Iterable[Union[Secret, str]]) -> str:
    """Заменяет в тексте значения секретов на ***."""
    values = [s.reveal() if isinstance(s, Secret) else s for s in secrets]
    for value in sorted((v for v in values if v and len(v) >= MIN_SECRET_CHARS), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


# --- настройки ---

def _str_list(cfg: dict, name: str) -> None:
    value = cfg.get(name, [])
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("tracker.%s должен быть списком строк" % name)


def _required_str(cfg: dict, name: str) -> None:
    if not isinstance(cfg.get(name), str) or not cfg[name].strip():
        raise ValueError("Не задано tracker.%s" % name)


def _http_url(value: Any, name: str) -> None:
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("tracker.%s должен быть http(s)-адресом" % name)
    if "@" in parts.netloc:
        raise ValueError("tracker.%s не должен содержать логин и пароль: секреты задаются через auth" % name)


def _validate_auth(auth: Any, kinds: tuple) -> None:
    if not isinstance(auth, dict) or auth.get("type") not in kinds:
        raise ValueError("tracker.auth.type должен быть одним из: %s" % ", ".join(kinds))
    allowed = {"token_env": {"type", "env", "user_env"}, "cookie_file": {"type", "path"}}[auth["type"]]
    extra = set(auth) - allowed
    if extra:
        raise ValueError("Недопустимые ключи tracker.auth: %s — секреты задаются только именем переменной "
                         "окружения или путём к файлу" % ", ".join(sorted(map(str, extra))))
    if auth["type"] == "token_env":
        if not isinstance(auth.get("env"), str) or not auth["env"].strip():
            raise ValueError("Не задано tracker.auth.env — имя переменной окружения с токеном")
        if auth.get("user_env") is not None and (not isinstance(auth["user_env"], str) or not auth["user_env"]):
            raise ValueError("tracker.auth.user_env должен быть именем переменной окружения")
    elif not isinstance(auth.get("path"), str) or not auth["path"].strip():
        raise ValueError("Не задано tracker.auth.path — путь к файлу с cookie")


def _validate_template(template: Any) -> None:
    if not isinstance(template, str):
        raise ValueError("tracker.title_template должен быть строкой")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise ValueError("Некорректный tracker.title_template: %s" % e)
    for _, name, spec, conversion in parsed:
        if name is not None and (name not in TITLE_PLACEHOLDERS or spec or conversion):
            raise ValueError("Неизвестный плейсхолдер в tracker.title_template: {%s}. Допустимы: {key}, {title}" % name)


def _validate_statuses(statuses: Any) -> None:
    if not isinstance(statuses, dict):
        raise ValueError("tracker.statuses должен быть объектом «статус трекера → статус доски»")
    allowed = {s.value for s in Status if s != Status.BLOCKED}
    for name, value in statuses.items():
        if value is not None and value not in allowed:
            raise ValueError("Неизвестный статус доски в tracker.statuses[%r]: %r" % (name, value))


def validate_config(cfg: Any) -> None:
    """Проверяет настройки трекера; секреты в них хранить нельзя."""
    if not isinstance(cfg, dict) or cfg.get("type") not in TYPES:
        raise ValueError("tracker.type должен быть одним из: %s" % ", ".join(TYPES))
    kind = cfg["type"]
    if kind == "none":
        return
    extra = set(cfg) - (JIRA_KEYS if kind == "jira" else GITHUB_KEYS)
    if extra:
        raise ValueError("Недопустимые ключи tracker: %s" % ", ".join(sorted(map(str, extra))))
    if kind == "jira":
        _http_url(cfg.get("url"), "url")
        _required_str(cfg, "project")
        _required_str(cfg, "issue_type")
        for name in ("epic", "epic_field", "link_type"):
            if cfg.get(name) is not None and not isinstance(cfg[name], str):
                raise ValueError("tracker.%s должен быть строкой" % name)
        _str_list(cfg, "components")
        if not isinstance(cfg.get("extra_fields", {}), dict):
            raise ValueError("tracker.extra_fields должен быть объектом")
        _validate_auth(cfg.get("auth"), ("token_env", "cookie_file"))
    else:
        if not isinstance(cfg.get("repo"), str) or not _REPO.match(cfg["repo"]):
            raise ValueError("tracker.repo должен быть в виде owner/name")
        if cfg.get("api_url") is not None:
            _http_url(cfg["api_url"], "api_url")
        _validate_auth(cfg.get("auth"), ("token_env",))
    _str_list(cfg, "labels")
    _validate_statuses(cfg.get("statuses", {}))
    _validate_template(cfg.get("title_template", "{title}"))
    poll = cfg.get("poll_seconds")
    if poll is not None and (isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0):
        raise ValueError("tracker.poll_seconds должен быть положительным числом секунд")


# --- авторизация ---

class _Auth:
    def __init__(self, headers: Dict[str, Secret], secrets: List[Secret], hint: str):
        self.headers = headers
        self.secrets = secrets
        self.hint = hint


def _env(name: str, what: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise TrackerError("Не задана переменная окружения %s (%s трекера)" % (name, what), "config")
    _check_header_text(value, "переменной окружения %s" % name)
    return value


def _check_header_text(value: str, where: str) -> None:
    """Значение уходит в заголовок: управляющие и не латинские символы отвергаются, не показывая само значение."""
    if any(ord(c) < 0x20 or ord(c) == 0x7f or ord(c) > 0xff for c in value):
        raise TrackerError("Недопустимые символы в %s (переводы строк, управляющие или не латинские символы)" % where,
                           "config")


def _load_auth(auth: dict, kind: str) -> _Auth:
    if auth["type"] == "cookie_file":
        path = Path(os.path.expanduser(auth["path"]))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            raise TrackerError("Не удалось прочитать файл с cookie трекера: %s" % auth["path"], "config") from None
        cookie = "; ".join(line.strip().rstrip(";").strip() for line in lines if line.strip())
        if not cookie:
            raise TrackerError("Файл с cookie трекера пуст: %s" % auth["path"], "config")
        _check_header_text(cookie, "файле с cookie %s" % auth["path"])
        parts = [p.partition("=")[2].strip() for p in cookie.split(";")]
        return _Auth({"Cookie": Secret(cookie)}, [Secret(cookie)] + [Secret(p) for p in parts if p],
                     "проверьте cookie в файле %s" % auth["path"])
    token = _env(auth["env"], "токен")
    hint = "проверьте токен в переменной окружения %s" % auth["env"]
    if kind == "jira" and auth.get("user_env"):
        user = _env(auth["user_env"], "пользователь")
        encoded = base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
        return _Auth({"Authorization": Secret("Basic " + encoded)}, [Secret(token), Secret(encoded)], hint)
    return _Auth({"Authorization": Secret("Bearer " + token)}, [Secret(token)], hint)


# --- HTTP ---

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправления не выполняются: иначе заголовки авторизации ушли бы на другой адрес."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _tracker_message(raw: bytes) -> str:
    """Текст ошибки из ответа Jira (errorMessages, errors) или GitHub (message, errors)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace").strip()
    if not isinstance(data, dict):
        return ""
    parts: List[str] = []
    for item in data.get("errorMessages") or []:
        parts.append(str(item))
    if isinstance(data.get("message"), str):
        parts.append(data["message"])
    errors = data.get("errors")
    if isinstance(errors, dict):
        parts.extend("%s: %s" % (k, v) for k, v in errors.items())
    elif isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                parts.append(str(item.get("message") or " ".join(
                    str(item[k]) for k in ("resource", "field", "code") if k in item)))
            else:
                parts.append(str(item))
    return "; ".join(p for p in parts if p)


class _Http:
    def __init__(self, base: str, auth: _Auth, headers: Optional[Dict[str, str]] = None):
        self.base = base.rstrip("/")
        self.auth = auth
        self.headers = dict(headers or {})
        self._opener = urllib.request.build_opener(_NoRedirect)

    def scrub(self, text: str) -> str:
        return scrub(text, self.auth.secrets)

    def _fail(self, message: str, kind: str) -> TrackerError:
        text = self.scrub(message)
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "…"
        return TrackerError(text, kind)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        headers = dict(self.headers, Accept="application/json")
        headers.setdefault("User-Agent", "agent-board")
        headers.update({k: v.reveal() for k, v in self.auth.headers.items()})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise self._http_error(e.code, e.read() if e.fp is not None else b"") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise self._fail("Трекер не ответил за %d с" % TIMEOUT, "network") from None
            raise self._fail("Нет связи с трекером %s: %s" % (self.base, e.reason), "network") from None
        except (socket.timeout, OSError) as e:
            raise self._fail("Нет связи с трекером %s: %s" % (self.base, e), "network") from None
        except ValueError:
            # текст такой ошибки содержит значение заголовка — не показываем его
            raise TrackerError("Некорректный запрос к трекеру: проверьте адрес и %s" % self.auth.hint,
                               "config") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise self._fail("Трекер вернул ответ не в JSON (%s %s)" % (method, path), "error") from None

    def _http_error(self, code: int, raw: bytes) -> TrackerError:
        message = _tracker_message(raw)
        detail = (": " + message) if message else ""
        if code in (401, 403):
            return self._fail("Трекер отклонил авторизацию (HTTP %d): %s%s" % (code, self.auth.hint, detail), "auth")
        if 300 <= code < 400:
            return self._fail("Трекер перенаправил запрос (HTTP %d) — проверьте адрес трекера и авторизацию: %s"
                              % (code, self.auth.hint), "auth")
        if code == 404:
            return self._fail("Не найдено в трекере (HTTP 404)%s" % detail, "not_found")
        if code in (400, 409, 422):
            return self._fail("Трекер отклонил запрос (HTTP %d)%s" % (code, detail), "validation")
        if code == 429 or code >= 500:
            return self._fail("Трекер временно недоступен (HTTP %d), повторите позже" % code, "retryable")
        return self._fail("Ошибка трекера (HTTP %d)%s" % (code, detail), "error")


# --- адаптеры ---

def issue_title(task: Task, template: str) -> str:
    return template.format_map({"key": task.key, "title": task.title})


def issue_text(task: Task, dep_refs: Iterable[str] = ()) -> str:
    """Описание задачи в трекере: описание, критерий готовности и зависимости."""
    parts = []
    if task.description.strip():
        parts.append(task.description.strip())
    if task.done_when.strip():
        parts.append("Критерий готовности: " + task.done_when.strip())
    refs = list(dep_refs)
    if refs:
        parts.append("Зависит от: " + ", ".join(refs))
    return "\n\n".join(parts)


class TrackerAdapter:
    # True — зависимости, упомянутые в тексте задачи, и есть связи (отдельных связей трекер не умеет)
    links_in_body = False

    def create_issue(self, task: Task, dep_refs: Iterable[str] = ()) -> Dict[str, str]:
        raise NotImplementedError

    def link(self, blocker_key: str, blocked_key: str) -> None:
        raise NotImplementedError

    def get_statuses(self, keys: List[str]) -> Dict[str, str]:
        raise NotImplementedError

    def check(self) -> None:
        raise NotImplementedError


class NoneTracker(TrackerAdapter):
    def _none(self, *args, **kwargs):
        raise TrackerError("В проекте не настроен трекер (tracker.type = none)", "config")

    create_issue = link = get_statuses = check = _none


class JiraTracker(TrackerAdapter):
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["url"].rstrip("/")
        self.http = _Http(self.base, _load_auth(cfg["auth"], "jira"))

    def create_issue(self, task: Task, dep_refs: Iterable[str] = ()) -> Dict[str, str]:
        cfg = self.cfg
        fields: Dict[str, Any] = {
            "project": {"key": cfg["project"]},
            "summary": issue_title(task, cfg.get("title_template") or "{title}"),
            "issuetype": {"name": cfg["issue_type"]},
            "description": issue_text(task, dep_refs),
        }
        if cfg.get("components"):
            fields["components"] = [{"name": c} for c in cfg["components"]]
        if cfg.get("labels"):
            fields["labels"] = list(cfg["labels"])
        if cfg.get("epic"):
            if cfg.get("epic_field"):
                fields[cfg["epic_field"]] = cfg["epic"]
            else:
                fields["parent"] = {"key": cfg["epic"]}
        fields.update(cfg.get("extra_fields") or {})
        result = self.http.call("POST", "/rest/api/2/issue", {"fields": fields})
        key = result.get("key") if isinstance(result, dict) else None
        if not isinstance(key, str) or not key:
            raise TrackerError("Jira не вернула ключ созданной задачи", "error")
        return {"key": key, "url": "%s/browse/%s" % (self.base, key)}

    def link(self, blocker_key: str, blocked_key: str) -> None:
        # в REST-ссылке inwardIssue получает исходящее описание типа: «blocker blocks blocked»
        self.http.call("POST", "/rest/api/2/issueLink", {
            "type": {"name": self.cfg.get("link_type") or "Blocks"},
            "inwardIssue": {"key": blocker_key},
            "outwardIssue": {"key": blocked_key},
        })

    def get_statuses(self, keys: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for i in range(0, len(keys), JIRA_SEARCH_BATCH):
            batch = keys[i:i + JIRA_SEARCH_BATCH]
            jql = "key in (%s)" % ", ".join(json.dumps(k) for k in batch)
            query = urllib.parse.urlencode({"jql": jql, "fields": "status", "maxResults": len(batch)})
            data = self.http.call("GET", "/rest/api/2/search?" + query) or {}
            for issue in data.get("issues") or []:
                status = ((issue.get("fields") or {}).get("status") or {}).get("name")
                if isinstance(issue.get("key"), str) and isinstance(status, str):
                    result[issue["key"]] = status
        return result

    def check(self) -> None:
        self.http.call("GET", "/rest/api/2/myself")
        try:
            self.http.call("GET", "/rest/api/2/project/" + urllib.parse.quote(self.cfg["project"], safe=""))
        except TrackerError as e:
            if e.kind == "not_found":
                raise TrackerError("Проект %s не найден в Jira или нет доступа к нему" % self.cfg["project"],
                                   "not_found") from None
            raise


class GitHubTracker(TrackerAdapter):
    links_in_body = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.repo = cfg["repo"]
        self.http = _Http(cfg.get("api_url") or GITHUB_API, _load_auth(cfg["auth"], "github"),
                          {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def _number(self, key: str) -> int:
        if not re.fullmatch(r"#\d+", key or ""):
            raise TrackerError("Некорректный ключ задачи GitHub: %r" % key, "error")
        return int(key[1:])

    def create_issue(self, task: Task, dep_refs: Iterable[str] = ()) -> Dict[str, str]:
        result = self.http.call("POST", "/repos/%s/issues" % self.repo, {
            "title": issue_title(task, self.cfg.get("title_template") or "{title}"),
            "body": issue_text(task, dep_refs),
            "labels": list(self.cfg.get("labels") or []),
        })
        if not isinstance(result, dict) or not isinstance(result.get("number"), int):
            raise TrackerError("GitHub не вернул номер созданной задачи", "error")
        return {"key": "#%d" % result["number"], "url": str(result.get("html_url") or "")}

    def link(self, blocker_key: str, blocked_key: str) -> None:
        self.http.call("POST", "/repos/%s/issues/%d/comments" % (self.repo, self._number(blocked_key)),
                       {"body": "Зависит от: %s" % blocker_key})

    def get_statuses(self, keys: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for key in keys:
            data = self.http.call("GET", "/repos/%s/issues/%d" % (self.repo, self._number(key))) or {}
            if isinstance(data.get("state"), str):
                result[key] = data["state"]
        return result

    def check(self) -> None:
        try:
            self.http.call("GET", "/repos/" + self.repo)
        except TrackerError as e:
            if e.kind == "not_found":
                raise TrackerError("Репозиторий %s не найден на GitHub или нет доступа к нему" % self.repo,
                                   "not_found") from None
            raise


def adapter_for(cfg: Any) -> TrackerAdapter:
    """Адаптер по настройкам трекера; секреты читаются здесь же."""
    validate_config(cfg)
    if cfg["type"] == "jira":
        return JiraTracker(cfg)
    if cfg["type"] == "github":
        return GitHubTracker(cfg)
    return NoneTracker()


# --- действия пользователя ---

_running: Dict[str, threading.Lock] = {}
_running_guard = threading.Lock()


def _project_lock(board) -> threading.Lock:
    with _running_guard:
        return _running.setdefault(str(board.store.root), threading.Lock())


def _require_user(actor: str, message: str) -> None:
    if actor != "user":
        raise PermissionError(message)


def check(config) -> None:
    """Проверяет доступ к трекеру проекта; при ошибке — TrackerError."""
    adapter_for(config.tracker).check()


def check_access(board, actor: str) -> dict:
    """Проверка доступа по запросу пользователя: {ok, error, kind}."""
    _require_user(actor, "Проверяет доступ к трекеру только пользователь")
    try:
        check(board.store.load_config())
    except TrackerError as e:
        return {"ok": False, "error": str(e), "kind": e.kind}
    return {"ok": True}


def create_issues(board, actor: str) -> dict:
    """Заводит в трекере задачи без ключа (зависимости раньше зависимых), затем недостающие связи.

    Уже заведённые задачи и связи пропускаются, поэтому повтор после сбоя продолжает с несделанного.
    Возвращает {created: [{key, tracker_key, url}], linked: [[blocker, blocked]], errors: [{key, error}], events}.
    """
    _require_user(actor, "Заводит задачи в трекере только пользователь")
    config = board.store.load_config()
    if config.stage != "tracker":
        raise ValueError("Задачи заводятся в трекере на стадии tracker (сейчас %s)" % config.stage)
    if not isinstance(config.tracker, dict) or config.tracker.get("type", "none") == "none":
        raise ValueError("В проекте не настроен трекер (tracker.type = none)")
    adapter = adapter_for(config.tracker)
    lock = _project_lock(board)
    if not lock.acquire(blocking=False):
        raise ValueError("Задачи уже заводятся в трекере")
    try:
        return _create(board, adapter)
    finally:
        lock.release()


def _create(board, adapter: TrackerAdapter) -> dict:
    tasks = {k: t for k, t in board.store.load_tasks().items() if t.status != Status.CANCELLED}
    links = set(board.store.load_meta().get("tracker_links") or [])
    created: List[dict] = []
    linked: List[List[str]] = []
    errors: List[dict] = []
    events: list = []
    stopped = False

    def remember(blocker: str, blocked: str) -> None:
        links.add("%s>%s" % (tasks[blocker].tracker_key, tasks[blocked].tracker_key))
        board.update_meta({"tracker_links": sorted(links)})
        linked.append([blocker, blocked])

    order = dependency_order(list(tasks), tasks)
    for key in order:
        task = tasks[key]
        if task.tracker_key:
            continue
        deps = [d for d in task.deps if d in tasks]
        missing = [d for d in deps if not tasks[d].tracker_key]
        if missing:
            errors.append({"key": key, "error": "%s не заведена: сначала нужно завести %s" % (key, ", ".join(missing))})
            continue
        try:
            issue = adapter.create_issue(task, [tasks[d].tracker_key for d in deps])
        except TrackerError as e:
            errors.append({"key": key, "error": str(e)})
            if e.kind in ("auth", "config"):
                stopped = True
                break
            continue
        events.extend(board.update_task(key, "system", {"tracker_key": issue["key"], "tracker_url": issue["url"]}))
        task.tracker_key, task.tracker_url = issue["key"], issue["url"]
        created.append({"key": key, "tracker_key": issue["key"], "url": issue["url"]})
        if adapter.links_in_body:
            for dep in deps:
                remember(dep, key)

    if not stopped:
        for key in order:
            task = tasks[key]
            for dep in sorted(d for d in task.deps if d in tasks):
                if not task.tracker_key or not tasks[dep].tracker_key:
                    continue
                if "%s>%s" % (tasks[dep].tracker_key, task.tracker_key) in links:
                    continue
                try:
                    adapter.link(tasks[dep].tracker_key, task.tracker_key)
                except TrackerError as e:
                    errors.append({"key": key, "error": "Связь %s → %s: %s" % (dep, key, e)})
                    if e.kind in ("auth", "config"):
                        stopped = True
                        break
                    continue
                remember(dep, key)
            if stopped:
                break

    events.append(board.record("tracker_created", {
        "created": [c["key"] for c in created], "linked": linked, "errors": [e["key"] for e in errors]}))
    return {"created": created, "linked": linked, "errors": errors, "events": [e.to_dict() for e in events]}
