"""Адаптеры ревью: crit (интерактивные раунды в браузере) и «без инструмента» (только дифф на доске).

Раунд crit — это процесс `crit --no-open`, который блокируется, пока пользователь не нажмёт «Finish Review».
Ссылка и id сессии приходят в stderr сразу после старта, вердикт — строкой `approved: true|false` в конце.
Повторный запуск с `--session` подключается к живой странице ревью и открывает следующий раунд;
если страницы уже нет, crit отвечает «no review found for session», и раунд открывается заново
(комментарии лежат в каталоге данных и не теряются, а тот же порт даёт ту же ссылку).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

from .harness import signal_group

REVIEW_TOOLS = ("crit", "none")
DEFAULT_BINARY = "crit"
COMMENTS_TIMEOUT = 30.0
# crit записывает комментарии на диск с задержкой: пустой список перечитывается один раз
COMMENTS_RETRY_DELAY = 1.5
OUTPUT_TAIL = 4000
_URL_LINE = re.compile(r"crit daemon at (\S+) \(session ([A-Za-z0-9_.-]+)")
_APPROVED_LINE = re.compile(r"^approved:\s*(true|false)\s*$")
_PORT = re.compile(r"^https?://[^/:]+:(\d+)")
_UNRESOLVED_COUNT = re.compile(r"finished with (\d+) unresolved comments")
NO_SESSION_MARK = "no review found for session"
DAEMON_GONE_MARK = "shut down before review was finished"
# лог ревью дописывается раундами: при открытии раунда от него остаётся столько последних байт
LOG_LIMIT_BYTES = 1024 * 1024
# данные ревью crit закрытой задачи хранятся столько дней после закрытия
REVIEW_DATA_KEEP_DAYS = 7
_SESSION = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
CLOSED_STATUSES = ("done", "cancelled")
# переменные, которые отключают у crit сетевую проверку обновлений и подсказки об интеграциях
QUIET_ENV = {"CRIT_NO_UPDATE_CHECK": "1", "CRIT_NO_INTEGRATION_CHECK": "1"}


class ReviewError(Exception):
    pass


@dataclass
class RoundResult:
    """outcome: approved | changes | aborted (страница ревью закрылась до завершения) |
    no_session (сессии для переподключения нет) | failed."""

    outcome: str
    comments: List[dict] = field(default_factory=list)
    error: str = ""
    url: Optional[str] = None
    session: Optional[str] = None


class _NotStarted:
    """Заглушка процесса раунда, который не удалось запустить."""

    pid = None
    args: List[str] = []

    def __init__(self, error: str):
        self.error = error

    def poll(self):
        return 127

    def wait(self, timeout=None):
        return 127


class NoReview:
    tool = "none"
    interactive = False


class CritReview:
    tool = "crit"
    interactive = True

    def __init__(self, binary: str = DEFAULT_BINARY):
        self.binary = binary or DEFAULT_BINARY

    def build_args(self, base_branch: str, data_dir: Path, session: Optional[str], port: Optional[int]) -> List[str]:
        args = [self.binary, "--no-open", "--base-branch", base_branch, "-o", str(data_dir)]
        if port:
            args += ["-p", str(port)]
        if session:
            args += ["--session", session]
        return args

    def start(self, cwd: Path, base_branch: str, data_dir: Path, session: Optional[str] = None,
              port: Optional[int] = None, env: Optional[dict] = None):
        """Запускает раунд в собственной группе процессов; при неудаче — заглушка с текстом ошибки."""
        full_env = dict(os.environ)
        full_env.update(QUIET_ENV)
        full_env.update(env or {})
        if shutil.which(self.binary, path=full_env.get("PATH")) is None:
            return _NotStarted("%s не найден" % self.binary)
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                self.build_args(base_branch, data_dir, session, port), cwd=str(cwd), env=full_env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError as e:
            return _NotStarted("не удалось запустить %s: %s" % (self.binary, e))
        proc.review_cwd = Path(cwd)
        proc.review_data_dir = Path(data_dir)
        return proc

    def watch(self, proc, log: Path, on_url: Callable[[str, str], None]) -> RoundResult:
        """Ждёт конца раунда; ссылку сообщает on_url, как только crit её напечатал."""
        if isinstance(proc, _NotStarted):
            return RoundResult("failed", error=proc.error)
        out: List[str] = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
        reader.start()
        url = session = verdict = None
        tail = ""
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(str(log), "a", encoding="utf-8") as f:
                for line in proc.stderr:
                    f.write(line if line.endswith("\n") else line + "\n")
                    f.flush()
                    tail = (tail + line)[-OUTPUT_TAIL:]
                    m = _URL_LINE.search(line)
                    if m and url is None:
                        url, session = m.group(1), m.group(2)
                        on_url(url, session)
                    v = _APPROVED_LINE.match(line.strip())
                    if v:
                        verdict = v.group(1) == "true"
                code = proc.wait()
                reader.join(COMMENTS_TIMEOUT)
                stdout = out[0] if out else ""
                f.write(stdout if stdout.endswith("\n") or not stdout else stdout + "\n")
        except BaseException:
            kill_round(proc)
            raise
        if NO_SESSION_MARK in tail:
            return RoundResult("no_session", error=tail.strip(), session=session)
        if DAEMON_GONE_MARK in stdout:
            return RoundResult("aborted", error=stdout.strip(), url=url, session=session)
        if verdict is None:
            error = tail.strip() or stdout.strip()[-OUTPUT_TAIL:] or "crit завершился с кодом %d" % code
            return RoundResult("failed", error=error, url=url, session=session)
        if verdict:
            return RoundResult("approved", url=url, session=session)
        comments = self._round_comments(proc, stdout)
        if comments is None:
            return RoundResult("failed", error="crit не одобрил раунд, но прочитать его комментарии не удалось",
                               url=url, session=session)
        return RoundResult("approved" if not comments else "changes", comments=comments, url=url, session=session)

    def _round_comments(self, proc, stdout: str) -> Optional[List[dict]]:
        """Комментарии неодобренного раунда; [] — только если и crit в выводе раунда показал ноль; None — не прочитаны."""
        printed = _comments_from_output(stdout)
        m = _UNRESOLVED_COUNT.search(stdout)
        count = int(m.group(1)) if m else None
        printed_zero = (count is not None or printed is not None) and not count and not printed
        for attempt in range(2):
            try:
                listed = self.fetch_comments(proc.review_cwd, proc.review_data_dir)
            except ReviewError:
                listed = None
            if listed:
                return listed
            # тот же список crit печатает в stdout для агента
            if printed:
                return printed
            if listed == [] and printed_zero:
                return []
            if attempt == 0:
                time.sleep(COMMENTS_RETRY_DELAY)
        return None

    def fetch_comments(self, cwd: Path, data_dir: Path) -> List[dict]:
        """Нерешённые комментарии ревью в виде {id, path, line, body}."""
        env = dict(os.environ)
        env.update(QUIET_ENV)
        try:
            res = subprocess.run([self.binary, "comments", "--json", "-o", str(data_dir)], cwd=str(cwd), env=env,
                                 capture_output=True, text=True, timeout=COMMENTS_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ReviewError("crit comments: %s" % e)
        if res.returncode != 0:
            raise ReviewError("crit comments: %s" % (res.stderr.strip() or res.returncode))
        try:
            raw = json.loads(res.stdout or "[]")
        except ValueError as e:
            raise ReviewError("crit comments: некорректный JSON: %s" % e)
        if not isinstance(raw, list):
            raise ReviewError("crit comments: ожидался список")
        return normalize_comments(raw)


    def stop_daemon(self, cwd: Path, data_dir: Path, session: str) -> bool:
        """Закрывает страницу ревью сессии; True — демон найден, опознан и остановлен."""
        env = dict(os.environ)
        env.update(QUIET_ENV)
        try:
            res = subprocess.run([self.binary, "status", "--json", "-o", str(data_dir)], cwd=str(cwd), env=env,
                                 capture_output=True, text=True, timeout=COMMENTS_TIMEOUT)
            status = json.loads(res.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        sessions = status.get("sessions") if isinstance(status, dict) else None
        entry = next((x for x in sessions or [] if isinstance(x, dict) and x.get("id") == session), None)
        pid = entry.get("pid") if entry else None
        if not isinstance(pid, int) or pid <= 0:
            return False
        # pid мог достаться другому процессу: останавливаем только демон crit с нашим каталогом данных
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                     timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        if "_serve" not in command or str(data_dir) not in command:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True


def _comments_from_output(stdout: str) -> Optional[List[dict]]:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("["):
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if isinstance(raw, list):
                return normalize_comments(raw)
    return None


def normalize_comments(raw: list) -> List[dict]:
    result = []
    for c in raw:
        if not isinstance(c, dict) or c.get("resolved") is True or not c.get("id"):
            continue
        line = c.get("start_line")
        result.append({
            "id": str(c["id"]),
            "path": c.get("path") or None,
            "line": line if isinstance(line, int) and line > 0 else None,
            "body": str(c.get("body") or ""),
        })
    return result


def make_review(config: dict):
    config = config if isinstance(config, dict) else {}
    tool = config.get("tool") or "crit"
    if tool == "none":
        return NoReview()
    if tool == "crit":
        return CritReview(config.get("binary") or DEFAULT_BINARY)
    raise ValueError("Неизвестный инструмент ревью: %s" % tool)


def port_of(url: Optional[str]) -> Optional[int]:
    m = _PORT.match(url or "")
    return int(m.group(1)) if m else None


def kill_round(proc) -> None:
    """Останавливает клиент раунда (наш дочерний процесс) без SIGTERM, как kill_group."""
    signal_group(proc, signal.SIGKILL)


def kill_group(pgid: Optional[int]) -> None:
    """Останавливает клиент раунда без SIGTERM: по SIGTERM crit закрыл бы и страницу ревью у пользователя."""
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def trim_log(path: Path, limit: Optional[int] = None) -> None:
    """Оставляет в логе последние limit байт (по умолчанию LOG_LIMIT_BYTES), начиная с целой строки."""
    limit = LOG_LIMIT_BYTES if limit is None else limit
    path = Path(path)
    try:
        size = path.stat().st_size
        if size <= limit:
            return
        with path.open("rb") as f:
            f.seek(size - limit)
            data = f.read()
    except OSError:
        return
    cut = data.find(b"\n") + 1
    tmp = path.with_name(path.name + ".trim")
    try:
        tmp.write_bytes(data[cut:] if cut else data)
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def prune_review_data(store, now: datetime, keep_days: int = REVIEW_DATA_KEEP_DAYS, skip=()) -> List[str]:
    """Удаляет данные ревью crit у задач, закрытых (done/cancelled) больше keep_days дней назад; skip — ключи задач,
    которые не трогать. Возвращает ключи задач, чьи данные удалены."""
    crit = Path(store.root) / "crit"
    reviews = crit / "reviews"
    sessions = crit / "sessions"
    if not reviews.is_dir() and not sessions.is_dir():
        return []
    candidates = {}
    for key, task in store.load_tasks().items():
        session = task.review.get("session") if isinstance(task.review, dict) else None
        if key in skip or task.status.value not in CLOSED_STATUSES or not isinstance(session, str) \
                or not _SESSION.match(session) or ".." in session:
            continue
        data = reviews / session
        if data.is_dir() or _session_files(sessions, session):
            candidates[key] = (data, task)
    if not candidates:
        return []
    closed_at = {}
    for event in store.read_events():
        if event.type == "status_changed" and event.data.get("key") in candidates \
                and event.data.get("to") in CLOSED_STATUSES:
            closed_at[event.data["key"]] = event.at
    removed = []
    for key, (data, task) in sorted(candidates.items()):
        moment = _parse_time(closed_at.get(key) or task.updated_at)
        if moment is None or now - moment < timedelta(days=keep_days):
            continue
        shutil.rmtree(str(data), ignore_errors=True)
        files = _session_files(sessions, task.review["session"])
        for path in files:
            try:
                path.unlink()
            except OSError:
                pass
        if not data.exists() and not any(p.exists() for p in files):
            removed.append(key)
    return removed


def _session_files(sessions: Path, session: str) -> List[Path]:
    """Существующие служебные файлы сессии crit (состояние и лог)."""
    return [p for p in (sessions / (session + ".json"), sessions / (session + ".log")) if p.exists()]
