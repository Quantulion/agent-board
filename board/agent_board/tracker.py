"""Трекеры задач (Jira, GitHub Issues): заведение задач доски, связи зависимостей, чтение статусов.

Секреты (токены, cookie) берутся только из переменных окружения или файлов, указанных в настройках,
и не попадают ни в настройки, ни в журнал, ни в тексты ошибок: всё, что уходит наружу, проходит через scrub.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import string
import sys
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from .messages import Msg, clip, msg, reason
from .model import Status, Task
from .ship import order as dependency_order
from .store import list_projects

TIMEOUT = 15.0
MAX_MESSAGE_CHARS = 500
# более короткие значения не маскируются, иначе текст превратится в звёздочки
MIN_SECRET_CHARS = 4
TITLE_PLACEHOLDERS = ("key", "title")
JIRA_SEARCH_BATCH = 50
TYPES = ("none", "jira", "github")
# в какие статусы доски трекер может переводить задачи
SYNC_TARGETS = ("not_started", "in_progress", "sent", "done")
JIRA_KEYS = {"type", "url", "project", "issue_type", "epic", "epic_field", "components", "labels", "extra_fields",
             "link_type", "auth", "statuses", "title_template", "poll_seconds", "jira_search_path"}
JIRA_SEARCH_PATH = "/rest/api/2/search"
# после этих ошибок следующие запросы бессмысленны: заведение останавливается
STOP_KINDS = ("auth", "config", "network")
GITHUB_KEYS = {"type", "repo", "labels", "auth", "statuses", "title_template", "api_url", "poll_seconds"}
GITHUB_API = "https://api.github.com"
JIRA_REQUIRED = ("url", "project", "issue_type", "auth")
GITHUB_REQUIRED = ("repo", "auth")
# допустимые ключи каждого вида авторизации и виды, которые принимает трекер
AUTH_KEYS = {"token_env": ("type", "env", "user_env"), "cookie_file": ("type", "path")}
AUTH_TYPES = {"jira": ("token_env", "cookie_file"), "github": ("token_env",)}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# формат ключа задачи в трекере каждого вида и пример для сообщения об ошибке
ISSUE_KEY_FORMATS = {
    "jira": (re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*"), "PROJ-123"),
    "github": (re.compile(r"#[1-9][0-9]*"), "#123"),
}


def check_issue_key(cfg: dict, key: str) -> None:
    """Проверяет, что ключ задачи похож на ключ трекера проекта, чтобы ошибка не всплыла только при синхронизации."""
    kind = cfg.get("type") if isinstance(cfg, dict) else None
    if kind not in ISSUE_KEY_FORMATS:
        return
    pattern, example = ISSUE_KEY_FORMATS[kind]
    if not pattern.fullmatch(key):
        name = "Jira" if kind == "jira" else "GitHub"
        raise ValueError("Ключ задачи %s должен быть вида %s, получено: %s" % (name, example, key))


class TrackerError(ValueError):
    """Ошибка трекера с текстом без секретов; kind: config, auth, not_found, validation, retryable, network, error."""

    def __init__(self, message: str, kind: str = "error", redirect: bool = False):
        super().__init__(message)
        self.kind = kind
        self.redirect = redirect


class SyncStopped(Exception):
    """Синхронизация прервана остановкой сервера."""


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


def _forms(value: str) -> List[str]:
    """Значение секрета и его виды в JSON-строке и в URL."""
    return [value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
            urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)]


def scrub(text: str, secrets: Iterable[Union[Secret, str]]) -> str:
    """Заменяет в тексте значения секретов (в том числе экранированные для JSON и URL) на ***."""
    values = set()
    for s in secrets:
        raw = s.reveal() if isinstance(s, Secret) else s
        if raw and len(raw) >= MIN_SECRET_CHARS:
            values.update(_forms(raw))
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


# --- настройки ---

def _str_list(cfg: dict, name: str) -> None:
    value = cfg.get(name, [])
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(msg("tracker.str_list", name=name))


def _required_str(cfg: dict, name: str) -> None:
    if not isinstance(cfg.get(name), str) or not cfg[name].strip():
        raise ValueError(msg("tracker.missing", name=name))


def _http_url(value: Any, name: str) -> None:
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(msg("tracker.http_url", name=name))
    if "@" in parts.netloc:
        raise ValueError(msg("tracker.url_credentials", name=name))


def _validate_auth(auth: Any, kinds: tuple) -> None:
    if not isinstance(auth, dict) or auth.get("type") not in kinds:
        raise ValueError(msg("tracker.auth_type", kinds=", ".join(kinds)))
    extra = set(auth) - set(AUTH_KEYS[auth["type"]])
    if extra:
        raise ValueError(msg("tracker.auth_extra", names=", ".join(sorted(map(str, extra)))))
    if auth["type"] == "token_env":
        if not isinstance(auth.get("env"), str) or not auth["env"].strip():
            raise ValueError(msg("tracker.auth_env"))
        if auth.get("user_env") is not None and (not isinstance(auth["user_env"], str) or not auth["user_env"]):
            raise ValueError(msg("tracker.auth_user_env"))
    elif not isinstance(auth.get("path"), str) or not auth["path"].strip():
        raise ValueError(msg("tracker.auth_path"))


def _validate_template(template: Any) -> None:
    if not isinstance(template, str):
        raise ValueError(msg("tracker.title_template_str"))
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as e:
        raise ValueError(msg("tracker.bad_title_template", error=e))
    for _, name, spec, conversion in parsed:
        if name is not None and (name not in TITLE_PLACEHOLDERS or spec or conversion):
            raise ValueError(msg("tracker.title_placeholder", name=name))


def _validate_statuses(statuses: Any) -> None:
    if not isinstance(statuses, dict):
        raise ValueError(msg("tracker.statuses_object"))
    for name, value in statuses.items():
        if value is not None and value not in SYNC_TARGETS:
            raise ValueError(msg("tracker.bad_status", name=name, value=value, allowed=", ".join(SYNC_TARGETS)))


def validate_config(cfg: Any) -> None:
    """Проверяет настройки трекера; секреты в них хранить нельзя."""
    if not isinstance(cfg, dict) or cfg.get("type") not in TYPES:
        raise ValueError(msg("tracker.type", types=", ".join(TYPES)))
    kind = cfg["type"]
    if kind == "none":
        return
    extra = set(cfg) - (JIRA_KEYS if kind == "jira" else GITHUB_KEYS)
    if extra:
        raise ValueError(msg("tracker.extra_keys", names=", ".join(sorted(map(str, extra)))))
    if kind == "jira":
        _http_url(cfg.get("url"), "url")
        _required_str(cfg, "project")
        _required_str(cfg, "issue_type")
        for name in ("epic", "epic_field", "link_type"):
            if cfg.get(name) is not None and not isinstance(cfg[name], str):
                raise ValueError(msg("tracker.field_str", name=name))
        _str_list(cfg, "components")
        path = cfg.get("jira_search_path")
        if path is not None and (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9/_.-]*", path)):
            raise ValueError(msg("tracker.search_path"))
        if not isinstance(cfg.get("extra_fields", {}), dict):
            raise ValueError(msg("tracker.extra_fields"))
        _validate_auth(cfg.get("auth"), AUTH_TYPES["jira"])
    else:
        if not isinstance(cfg.get("repo"), str) or not _REPO.match(cfg["repo"]):
            raise ValueError(msg("tracker.repo"))
        if cfg.get("api_url") is not None:
            _http_url(cfg["api_url"], "api_url")
        _validate_auth(cfg.get("auth"), AUTH_TYPES["github"])
    _str_list(cfg, "labels")
    _validate_statuses(cfg.get("statuses", {}))
    _validate_template(cfg.get("title_template", "{title}"))
    poll = cfg.get("poll_seconds")
    if poll is not None and (isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0):
        raise ValueError(msg("tracker.poll_seconds"))


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def insecure_warning(cfg: Any) -> Optional[str]:
    """Предупреждение, если трекер на чужом хосте доступен по http без шифрования; иначе None."""
    if not isinstance(cfg, dict):
        return None
    url = cfg.get("url") if cfg.get("type") == "jira" else cfg.get("api_url") if cfg.get("type") == "github" else None
    parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme.lower() != "http" or (parts.hostname or "").lower() in LOCAL_HOSTS:
        return None
    return msg("tracker.insecure", host=parts.hostname)


# --- авторизация ---

class _Auth:
    def __init__(self, headers: Dict[str, Secret], secrets: List[Secret], hint: Msg):
        self.headers = headers
        self.secrets = secrets
        self.hint = hint


def _env(name: str, what: Msg) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise TrackerError(msg("tracker.env_missing", name=name, what=what), "config")
    _check_header_text(value, msg("tracker.where_env", name=name))
    return value


def _check_header_text(value: str, where: Msg) -> None:
    """Значение уходит в заголовок: управляющие и не латинские символы отвергаются, не показывая само значение."""
    if any(ord(c) < 0x20 or ord(c) == 0x7f or ord(c) > 0xff for c in value):
        raise TrackerError(msg("tracker.bad_chars", where=where), "config")


def _load_auth(auth: dict, kind: str) -> _Auth:
    if auth["type"] == "cookie_file":
        path = Path(os.path.expanduser(auth["path"]))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            raise TrackerError(msg("tracker.cookie_unreadable", path=auth["path"]), "config") from None
        cookie = "; ".join(line.strip().rstrip(";").strip() for line in lines if line.strip())
        if not cookie:
            raise TrackerError(msg("tracker.cookie_empty", path=auth["path"]), "config")
        _check_header_text(cookie, msg("tracker.where_cookie", path=auth["path"]))
        parts = [p.partition("=")[2].strip() for p in cookie.split(";")]
        return _Auth({"Cookie": Secret(cookie)}, [Secret(cookie)] + [Secret(p) for p in parts if p],
                     msg("tracker.hint_cookie", path=auth["path"]))
    token = _env(auth["env"], msg("tracker.what_token"))
    hint = msg("tracker.hint_token", name=auth["env"])
    if kind == "jira" and auth.get("user_env"):
        user = _env(auth["user_env"], msg("tracker.what_user"))
        encoded = base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
        return _Auth({"Authorization": Secret("Basic " + encoded)}, [Secret(token), Secret(encoded)], hint)
    return _Auth({"Authorization": Secret("Bearer " + token)}, [Secret(token)], hint)


def secrets_of(cfg: Any) -> List[Secret]:
    """Секреты трекера проекта для маскирования чужого вывода; без настроенного или доступного секрета — []."""
    try:
        validate_config(cfg)
        if cfg["type"] == "none":
            return []
        return _load_auth(cfg["auth"], cfg["type"]).secrets
    except (TrackerError, ValueError):
        return []


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
        # Jira помечает ответы анонимному пользователю заголовком X-AUSERNAME: anonymous
        self.anonymous_seen = False

    def scrub(self, text: str) -> str:
        return scrub(text, self.auth.secrets)

    def _fail(self, message: Msg, kind: str, redirect: bool = False) -> TrackerError:
        # вставки из ответа трекера очищаются от секретов и обрезаются, текст каталога секретов не содержит
        params = {}
        for name, value in message.params.items():
            if isinstance(value, str) and not isinstance(value, Msg):
                value = self.scrub(value)
                if len(value) > MAX_MESSAGE_CHARS:
                    value = value[:MAX_MESSAGE_CHARS] + "…"
            params[name] = value
        return TrackerError(msg(message.key, **params), kind, redirect)

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
                if (resp.headers.get("X-AUSERNAME") or "").strip().lower() == "anonymous":
                    self.anonymous_seen = True
        except urllib.error.HTTPError as e:
            raise self._http_error(e.code, e.read() if e.fp is not None else b"") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise self._timeout() from None
            raise self._fail(msg("tracker.no_connection", base=self.base, reason=str(e.reason)), "network") from None
        except socket.timeout:
            raise self._timeout() from None
        except (OSError, http.client.HTTPException) as e:
            raise self._fail(msg("tracker.no_connection", base=self.base, reason=str(e) or type(e).__name__),
                             "network") from None
        except ValueError:
            # текст такой ошибки содержит значение заголовка — не показываем его
            raise TrackerError(msg("tracker.bad_request", hint=self.auth.hint), "config") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise self._fail(msg("tracker.not_json", method=method, path=path), "error") from None

    def _timeout(self) -> TrackerError:
        return self._fail(msg("tracker.timeout", seconds=TIMEOUT), "network")

    def object(self, method: str, path: str) -> dict:
        """Ответ, который должен быть JSON-объектом."""
        data = self.call(method, path)
        if not isinstance(data, dict):
            raise TrackerError(msg("tracker.unexpected", method=method, path=path.split("?")[0]), "validation")
        return data

    def _http_error(self, code: int, raw: bytes) -> TrackerError:
        message = _tracker_message(raw)
        detail = (": " + message) if message else ""
        if code in (401, 403):
            return self._fail(msg("tracker.auth_rejected", code=code, hint=self.auth.hint, detail=detail), "auth")
        if 300 <= code < 400:
            return self._fail(msg("tracker.redirect", code=code, hint=self.auth.hint), "auth", redirect=True)
        if code == 404:
            return self._fail(msg("tracker.not_found", detail=detail), "not_found")
        if code in (400, 409, 422):
            return self._fail(msg("tracker.rejected", code=code, detail=detail), "validation")
        if code == 429 or code >= 500:
            return self._fail(msg("tracker.unavailable", code=code), "retryable")
        return self._fail(msg("tracker.http_error", code=code, detail=detail), "error")


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

    def get_statuses(self, keys: List[str], errors: Optional[Dict[str, str]] = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, str]:
        """Статусы задач по ключам. Недоступные по отдельности задачи не прерывают чтение: их причины попадают в
        errors (если он передан); should_stop — признак остановки между запросами."""
        raise NotImplementedError

    def check(self) -> None:
        raise NotImplementedError


class NoneTracker(TrackerAdapter):
    def _none(self, *args, **kwargs):
        raise TrackerError(msg("tracker.not_configured"), "config")

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
            raise TrackerError(msg("tracker.jira_no_key"), "error")
        return {"key": key, "url": "%s/browse/%s" % (self.base, key)}

    def link(self, blocker_key: str, blocked_key: str) -> None:
        # в REST-ссылке inwardIssue получает исходящее описание типа: «blocker blocks blocked»
        self.http.call("POST", "/rest/api/2/issueLink", {
            "type": {"name": self.cfg.get("link_type") or "Blocks"},
            "inwardIssue": {"key": blocker_key},
            "outwardIssue": {"key": blocked_key},
        })

    def get_statuses(self, keys: List[str], errors: Optional[Dict[str, str]] = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, str]:
        result: Dict[str, str] = {}
        errors = errors if errors is not None else {}
        self.http.anonymous_seen = False
        for i in range(0, len(keys), JIRA_SEARCH_BATCH):
            self._batch(keys[i:i + JIRA_SEARCH_BATCH], result, errors, should_stop)
        # с просроченной сессией Jira отвечает анонимному пользователю: задачи «не находятся», а ошибки нет
        if keys and (not result or self.http.anonymous_seen):
            self._check_user()
        return result

    def _check_user(self) -> None:
        """Jira узнаёт пользователя; иначе — ошибка авторизации."""
        self.http.anonymous_seen = False
        self.http.call("GET", "/rest/api/2/myself")
        if self.http.anonymous_seen:
            raise TrackerError(msg("tracker.jira_anonymous", hint=self.http.auth.hint), "auth")

    def _batch(self, batch: List[str], result: Dict[str, str], errors: Dict[str, str],
               should_stop: Optional[Callable[[], bool]]) -> None:
        if should_stop is not None and should_stop():
            raise SyncStopped()
        jql = "key in (%s)" % ", ".join(json.dumps(k) for k in batch)
        # warn: несуществующие ключи не ломают весь запрос, а просто не возвращаются
        query = urllib.parse.urlencode({"jql": jql, "fields": "status", "maxResults": len(batch),
                                        "validateQuery": "warn"})
        try:
            data = self.http.call("GET", (self.cfg.get("jira_search_path") or JIRA_SEARCH_PATH) + "?" + query)
        except TrackerError as e:
            if e.kind not in ("validation", "not_found"):
                raise
            if len(batch) > 1:
                for key in batch:
                    self._batch([key], result, errors, should_stop)
            else:
                errors[batch[0]] = reason(e)
            return
        if not isinstance(data, dict):
            raise TrackerError(msg("tracker.unexpected", method="GET", path=JIRA_SEARCH_PATH), "validation")
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            raise TrackerError(msg("tracker.jira_search"), "validation")
        for issue in issues:
            fields = issue.get("fields") if isinstance(issue, dict) else None
            status = fields.get("status") if isinstance(fields, dict) else None
            name = status.get("name") if isinstance(status, dict) else None
            if isinstance(issue.get("key"), str) and isinstance(name, str):
                result[issue["key"]] = name
        for key in batch:
            if key not in result and key not in errors:
                errors[key] = msg("tracker.jira_issue_missing", key=key)

    def check(self) -> None:
        self._check_user()
        try:
            self.http.call("GET", "/rest/api/2/project/" + urllib.parse.quote(self.cfg["project"], safe=""))
        except TrackerError as e:
            if e.kind == "not_found":
                raise TrackerError(msg("tracker.jira_project_missing", project=self.cfg["project"]),
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
            raise TrackerError(msg("tracker.github_bad_key", key=key), "error")
        return int(key[1:])

    def create_issue(self, task: Task, dep_refs: Iterable[str] = ()) -> Dict[str, str]:
        result = self.http.call("POST", "/repos/%s/issues" % self.repo, {
            "title": issue_title(task, self.cfg.get("title_template") or "{title}"),
            "body": issue_text(task, dep_refs),
            "labels": list(self.cfg.get("labels") or []),
        })
        if not isinstance(result, dict) or not isinstance(result.get("number"), int):
            raise TrackerError(msg("tracker.github_no_number"), "error")
        return {"key": "#%d" % result["number"], "url": str(result.get("html_url") or "")}

    def link(self, blocker_key: str, blocked_key: str) -> None:
        self.http.call("POST", "/repos/%s/issues/%d/comments" % (self.repo, self._number(blocked_key)),
                       {"body": "Зависит от: %s" % blocker_key})

    def get_statuses(self, keys: List[str], errors: Optional[Dict[str, str]] = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, str]:
        result: Dict[str, str] = {}
        errors = errors if errors is not None else {}
        for key in keys:
            if should_stop is not None and should_stop():
                raise SyncStopped()
            try:
                data = self.http.object("GET", "/repos/%s/issues/%d" % (self.repo, self._number(key)))
            except TrackerError as e:
                # проблема одной задачи (нет, удалена, перенесена, кривой ключ) не мешает остальным
                if e.redirect or e.kind in ("not_found", "error"):
                    errors[key] = reason(e)
                    continue
                raise
            if isinstance(data.get("state"), str):
                result[key] = data["state"]
            else:
                errors[key] = msg("tracker.github_no_state", key=key)
        return result

    def check(self) -> None:
        try:
            self.http.call("GET", "/repos/" + self.repo)
        except TrackerError as e:
            if e.kind == "not_found":
                raise TrackerError(msg("tracker.github_repo_missing", repo=self.repo), "not_found") from None
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
    """Проверка доступа по запросу пользователя: {ok, error, kind, warning}."""
    _require_user(actor, msg("tracker.check_user_only"))
    config = board.store.load_config()
    warning = insecure_warning(config.tracker)
    try:
        check(config)
    except TrackerError as e:
        result = {"ok": False, "error": reason(e), "kind": e.kind}
    else:
        result = {"ok": True}
    if warning:
        result["warning"] = warning
    return result


def create_issues(board, actor: str) -> dict:
    """Заводит в трекере задачи без ключа (зависимости раньше зависимых), затем недостающие связи.

    Уже заведённые задачи и связи пропускаются, поэтому повтор после сбоя продолжает с несделанного.
    Возвращает {created: [{key, tracker_key, url}], linked: [[blocker, blocked]], errors: [{key, error}],
    warnings: [{key, warning}], events}.
    """
    _require_user(actor, msg("tracker.create_user_only"))
    config = board.store.load_config()
    if config.stage != "tracker":
        raise ValueError(msg("tracker.wrong_stage", stage=config.stage))
    if not isinstance(config.tracker, dict) or config.tracker.get("type", "none") == "none":
        raise ValueError(msg("tracker.not_configured"))
    adapter = adapter_for(config.tracker)
    lock = _project_lock(board)
    if not lock.acquire(blocking=False):
        raise ValueError(msg("tracker.creating"))
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
    warnings: List[dict] = []
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
            errors.append({"key": key, "error": msg("tracker.dep_not_created", key=key, missing=", ".join(missing))})
            continue
        try:
            issue = adapter.create_issue(task, [tasks[d].tracker_key for d in deps])
        except TrackerError as e:
            errors.append({"key": key, "error": reason(e)})
            if e.kind in STOP_KINDS:
                stopped = True
                break
            continue
        url = issue["url"] if is_http_url(issue["url"]) else None
        if url is None:
            warnings.append({"key": key, "warning": msg("tracker.bad_issue_url", key=issue["key"])})
        events.extend(board.update_task(key, "system", {"tracker_key": issue["key"], "tracker_url": url}))
        task.tracker_key, task.tracker_url = issue["key"], url
        created.append({"key": key, "tracker_key": issue["key"], "url": url})
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
                    errors.append({"key": key, "error": msg("tracker.link_failed", dep=dep, key=key, error=reason(e))})
                    if e.kind in STOP_KINDS:
                        stopped = True
                        break
                    continue
                remember(dep, key)
            if stopped:
                break

    events.append(board.record("tracker_created", {
        "created": [c["key"] for c in created], "linked": linked, "errors": [e["key"] for e in errors]}))
    return {"created": created, "linked": linked, "errors": errors, "warnings": warnings,
            "events": [e.to_dict() for e in events]}


def is_http_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# --- фоновая синхронизация статусов ---

# GitHub опрашивается по запросу на задачу, поэтому реже: лимит 5000 запросов в час
DEFAULT_POLL_SECONDS = {"jira": 30, "github": 120}
MAX_BACKOFF_SECONDS = 300
# как часто поток проверяет, не пора ли синхронизировать (и не изменились ли настройки трекера)
IDLE_SECONDS = 1.0
SYNC_STAGES = ("work", "done")
# после этих ошибок повторять бессмысленно до исправления настроек или доступа
PAUSE_KINDS = ("auth", "config")
# завершённый проект ещё столько опрашивается: задачи в трекере закрывают с опозданием
DONE_POLL_SECONDS = 24 * 3600
# столько проектов синхронизируются одновременно
SYNC_WORKERS = 4
# столько проход ждёт запущенные проекты; зависший проект досинхронизируется в фоне, остальные не ждут его
TICK_WAIT_SECONDS = 10.0
STOP_JOIN_SECONDS = 2.0


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(cfg: Any) -> str:
    """Отпечаток настроек трекера: меняется вместе с настройками, сами значения не раскрывает."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def sync_enabled(config) -> bool:
    return (isinstance(config.tracker, dict) and config.tracker.get("type", "none") != "none"
            and config.stage in SYNC_STAGES)


class TrackerSync:
    """Один поток на сервер: периодически читает статусы задач в трекерах проектов и переводит задачи доски.

    Результат последнего прохода хранится в meta проекта (`tracker_sync`) и попадает в снимок. События пишутся
    только при изменениях. После ошибок авторизации и настроек синхронизация проекта встаёт на паузу до изменения
    настроек трекера или ручного запуска.
    """

    def __init__(self, server, clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], Any]] = None,
                 wall_clock: Optional[Callable[[], datetime]] = None):
        self._server = server
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._slots = threading.BoundedSemaphore(SYNC_WORKERS)
        self._workers: Dict[str, threading.Thread] = {}
        self.tick_wait = TICK_WAIT_SECONDS
        self._last_started = ""
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._thread: Optional[threading.Thread] = None
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._due: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}

    # --- жизненный цикл ---

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="board-tracker-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None
        # проект, зависший на запросе к трекеру, не держит остановку: потоки фоновые
        deadline = time.monotonic() + STOP_JOIN_SECONDS
        with self._guard:
            workers = list(self._workers.values())
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.tick()
            except Exception as exc:
                print("tracker sync failed: %s" % type(exc).__name__, file=sys.stderr)
                delay = IDLE_SECONDS
            self._sleep(delay)

    # --- проходы ---

    def next_due(self, slug: str) -> Optional[float]:
        return self._due.get(slug)

    def _project_lock(self, slug: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(slug, threading.Lock())

    def tick(self) -> float:
        """Запускает синхронизацию проектов в пуле потоков и ждёт их не дольше tick_wait; возвращает, сколько ждать
        до следующей проверки."""
        started = []
        slugs = list_projects()
        # по кругу: при занятом пуле проекты в конце списка не должны ждать вечно
        cut = next((i for i, slug in enumerate(slugs) if slug > self._last_started), 0)
        for slug in slugs[cut:] + slugs[:cut]:
            if self._stop.is_set():
                break
            # поток заводится только проектам, которым пора синхронизироваться
            if not self._is_due(slug):
                continue
            lock = self._project_lock(slug)
            # проект, который ещё синхронизируется (вручную или прошлым проходом), пропускается до следующей проверки
            if not lock.acquire(blocking=False):
                continue
            if not self._slots.acquire(blocking=False):
                lock.release()
                continue
            worker = threading.Thread(target=self._worker, args=(slug, lock), name="board-tracker-sync-%s" % slug,
                                      daemon=True)
            with self._guard:
                self._workers[slug] = worker
            try:
                worker.start()
            except BaseException:
                with self._guard:
                    self._workers.pop(slug, None)
                self._slots.release()
                lock.release()
                raise
            started.append(worker)
            self._last_started = slug
        deadline = time.monotonic() + self.tick_wait
        for worker in started:
            while worker.is_alive() and not self._stop.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                worker.join(min(left, 0.1))
        return IDLE_SECONDS

    def _worker(self, slug: str, lock: threading.Lock) -> None:
        try:
            if not self._stop.is_set():
                self._tick_project(slug)
        except Exception as exc:
            print("tracker sync %s failed: %s" % (slug, type(exc).__name__), file=sys.stderr)
        finally:
            with self._guard:
                if self._workers.get(slug) is threading.current_thread():
                    del self._workers[slug]
            self._slots.release()
            lock.release()

    def _is_due(self, slug: str) -> bool:
        """Нужен ли проекту проход сейчас: включена синхронизация, проект не завершён давно, не на паузе и подошёл срок."""
        try:
            board = self._server.hub.board(slug)
            config = board.store.load_config()
        except (KeyError, ValueError, OSError):
            return False
        if not sync_enabled(config):
            return False
        try:
            meta = board.store.load_meta()
            if config.stage == "done" and self._long_done(board, meta):
                return False
        except (ValueError, OSError):
            return False
        if meta.get("tracker_sync_fp") not in (None, _fingerprint(config.tracker)):
            return True
        if (meta.get("tracker_sync") or {}).get("paused"):
            return False
        now = self._clock()
        return now >= self._due.get(slug, now)

    def _tick_project(self, slug: str) -> None:
        now = self._clock()
        try:
            board = self._server.hub.board(slug)
            config = board.store.load_config()
        except (KeyError, ValueError, OSError):
            return
        if not sync_enabled(config):
            return
        meta = board.store.load_meta()
        if config.stage == "done" and self._long_done(board, meta):
            return
        state = meta.get("tracker_sync") or {}
        if meta.get("tracker_sync_fp") not in (None, _fingerprint(config.tracker)):
            self._due.pop(slug, None)
            self._failures.pop(slug, None)
        elif state.get("paused"):
            return
        if now >= self._due.get(slug, now):
            self._run(slug, board, config, now)

    def _long_done(self, board, meta: dict) -> bool:
        """Проект завершён больше суток назад. Время завершения берётся из журнала и запоминается в meta."""
        done_at = meta.get("stage_done_at")
        if not isinstance(done_at, str):
            done_at = next((e.at for e in reversed(board.store.read_events())
                            if e.type == "stage_changed" and e.data.get("to") == "done"), None)
            if done_at is None:
                return True
            board.update_meta({"stage_done_at": done_at})
        try:
            moment = datetime.fromisoformat(done_at)
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (self._wall_clock() - moment).total_seconds() > DONE_POLL_SECONDS

    def sync_now(self, board, actor: str) -> dict:
        """Ручной запуск («Повторить сейчас»): один проход проекта сейчас, при успехе пауза снимается."""
        _require_user(actor, msg("tracker.sync_user_only"))
        config = board.store.load_config()
        if not isinstance(config.tracker, dict) or config.tracker.get("type", "none") == "none":
            raise ValueError(msg("tracker.not_configured"))
        if config.stage not in SYNC_STAGES:
            raise ValueError(msg("tracker.sync_stage", stage=config.stage))
        slug = board.store.root.name
        with self._project_lock(slug):
            return self._run(slug, board, config, self._clock())

    def _run(self, slug: str, board, config, now: float) -> dict:
        interval = config.tracker.get("poll_seconds") or DEFAULT_POLL_SECONDS.get(config.tracker.get("type"), 30)
        previous = board.store.load_meta().get("tracker_sync") or {}
        state = {"at": _now_text(), "ok": True, "error": None, "paused": False, "unknown_statuses": [],
                 "changed": [], "errors": [], "warning": insecure_warning(config.tracker)}
        try:
            self._sync_project(board, config, state)
            self._failures.pop(slug, None)
            self._due[slug] = now + interval
        except SyncStopped:
            return state
        except TrackerError as e:
            self._fail(slug, state, reason(e), e.kind in PAUSE_KINDS, now, interval)
        except ValueError as e:
            # некорректные настройки трекера
            self._fail(slug, state, reason(e), True, now, interval)
        except Exception as exc:
            print("tracker sync %s failed: %s" % (slug, type(exc).__name__), file=sys.stderr)
            self._fail(slug, state, msg("tracker.sync_internal"), False, now, interval)
        self._save(board, config, state, previous)
        return state

    def _fail(self, slug: str, state: dict, error: str, pause: bool, now: float, interval: float) -> None:
        state["ok"], state["error"] = False, clip(error, MAX_MESSAGE_CHARS)
        failures = self._failures.get(slug, 0)
        if pause:
            state["paused"] = True
            self._failures.pop(slug, None)
            self._due[slug] = now + interval
            return
        self._failures[slug] = failures + 1
        self._due[slug] = now + min(max(MAX_BACKOFF_SECONDS, interval), interval * 2 ** failures)

    def _sync_project(self, board, config, state: dict) -> None:
        adapter = adapter_for(config.tracker)
        tasks = {t.key: t for t in board.store.load_tasks().values() if t.tracker_key and t.status != Status.CANCELLED}
        meta = board.store.load_meta()
        # запоминаем, какой статус трекера видели у задачи (вместе с ключом в трекере)
        seen: Dict[str, list] = {k: v for k, v in (meta.get("tracker_seen") or {}).items()
                                 if k in tasks and isinstance(v, list) and len(v) == 2}
        if tasks:
            self._apply(board, config, adapter, tasks, seen, state)
        state["unknown_statuses"] = sorted(state["unknown_statuses"])
        if seen != (meta.get("tracker_seen") or {}):
            board.update_meta({"tracker_seen": seen})

    def _apply(self, board, config, adapter, tasks: Dict[str, Task], seen: Dict[str, list], state: dict) -> None:
        mapping = {str(name).strip().lower(): target for name, target in (config.tracker.get("statuses") or {}).items()}
        failed: Dict[str, str] = {}
        statuses = adapter.get_statuses([t.tracker_key for t in tasks.values()], failed, self._stop.is_set)
        unknown = set()
        for key in sorted(tasks):
            task = tasks[key]
            name = statuses.get(task.tracker_key)
            if not isinstance(name, str):
                if task.tracker_key in failed:
                    state["errors"].append({"key": key, "error": failed[task.tracker_key]})
                continue
            known = mapping.get(name.strip().lower(), "unknown")
            if known == "unknown":
                unknown.add(name)
            target = known if known in SYNC_TARGETS else None
            entry = seen.get(key)
            previous = entry[1] if entry and entry[0] == task.tracker_key else None
            # Новый для доски статус трекера только запоминается: доска ведёт задачу дальше трекера, и откатывать
            # её к отставшему статусу нельзя. Исключение — закрытая в трекере задача при первом чтении.
            if target is not None and target != task.status.value and (
                    (previous is not None and previous != name) or (previous is None and target == "done")):
                try:
                    events = board.transition_if(key, task.status, Status(target), "tracker")
                except (PermissionError, ValueError, KeyError) as e:
                    state["errors"].append({"key": key, "error": clip(reason(e), MAX_MESSAGE_CHARS)})
                    continue
                if events:
                    state["changed"].append(key)
            seen[key] = [task.tracker_key, name]
        state["unknown_statuses"] = unknown

    @staticmethod
    def _save(board, config, state: dict, previous: dict) -> None:
        board.update_meta({"tracker_sync": state, "tracker_sync_fp": _fingerprint(config.tracker)})
        keys = ("ok", "error", "paused", "unknown_statuses", "errors")
        if state["changed"] or any(state[k] != previous.get(k) for k in keys):
            board.record("tracker_synced", {"ok": state["ok"], "changed": state["changed"], "error": state["error"]})
