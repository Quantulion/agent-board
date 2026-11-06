from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agent_board import __version__
from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board import server as server_mod
from agent_board.server import DEFAULT_PORTS, BoardServer, ServerAlreadyRunning, read_discovery
from agent_board.model import Status
from agent_board.store import ProjectStore

from conftest import make_task


class Resp:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        return json.loads(self.body)


def call(server, method, path, body=None, token="default", headers=None):
    h = dict(headers or {})
    if token == "default":
        token = server.token
    if token:
        h["X-Board-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (server.port, path), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return Resp(r.status, r.read(), r.headers)
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read(), e.headers)


def get(server, path, **kw):
    return call(server, "GET", path, **kw)


def post(server, path, body=None, **kw):
    return call(server, "POST", path, body if body is not None else {}, **kw)


@pytest.fixture
def server(tmp_home):
    s = ProjectStore("pay")
    s.create(ProjectConfig(slug="pay", title="Платежи", chains=True))
    srv = BoardServer(port=0)
    srv.start()
    yield srv
    srv.stop()


def add_task(server, key="T-1", **extra):
    task = {"key": key, "title": key}
    task.update(extra)
    return post(server, "/api/p/pay/tasks", {"task": task, "actor": "user"})


def test_api_requires_token(server):
    assert get(server, "/api/projects", token=None).status == 401
    assert get(server, "/api/projects", token="wrong").status == 401
    assert get(server, "/api/projects").json() == {"projects": ["pay"]}


def test_post_with_foreign_origin_forbidden(server):
    r = post(server, "/api/p/pay/tasks", {"task": {"key": "T-1", "title": "x"}, "actor": "user"},
             headers={"Origin": "https://evil.example"})
    assert r.status == 403
    ok = post(server, "/api/p/pay/tasks", {"task": {"key": "T-1", "title": "x"}, "actor": "user"},
              headers={"Origin": "http://127.0.0.1:%d" % server.port})
    assert ok.status == 200


def test_foreign_host_header_forbidden(server):
    r = get(server, "/api/projects", headers={"Host": "evil.example"})
    assert r.status == 403
    r = get(server, "/api/projects", headers={"Host": "localhost:%d" % server.port})
    assert r.status == 200


def test_cookie_set_from_query_token(server):
    r = get(server, "/?token=%s" % server.token, token=None)
    assert r.status == 200
    cookie = r.headers["Set-Cookie"]
    assert "board_token=%s" % server.token in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Max-Age=%d" % (30 * 24 * 3600) in cookie
    assert '<script type="module" src="/static/js/main.js"></script>' in r.body.decode()
    # куки достаточно для API
    r2 = get(server, "/api/projects", token=None, headers={"Cookie": "board_token=%s" % server.token})
    assert r2.status == 200


def test_static_path_traversal_blocked(tmp_home, tmp_path):
    # arrange
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("hi")
    (tmp_path / "secret.txt").write_text("secret")
    srv = BoardServer(port=0, static_dir=static)
    srv.start()
    try:
        # act
        results = {}
        for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt", "/../secret.txt", "/static/index.html"):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            c.request("GET", path)
            r = c.getresponse()
            results[path] = (r.status, r.read())
    finally:
        srv.stop()
    # assert
    assert results["/static/../secret.txt"][0] == 404
    assert results["/static/%2e%2e/secret.txt"][0] == 404
    assert results["/../secret.txt"][0] == 404
    assert all(b"secret" not in body for _, body in results.values())
    assert results["/static/index.html"] == (200, b"hi")


def test_transition_roundtrip_and_validation_error_is_400(server):
    assert add_task(server).status == 200
    r = post(server, "/api/p/pay/tasks/T-1/transition", {"to": "in_progress", "actor": "agent"})
    assert r.status == 200
    assert any(e["type"] == "status_changed" for e in r.json()["events"])
    state = get(server, "/api/p/pay/state").json()
    assert state["tasks"][0]["status"] == "in_progress"
    bad = post(server, "/api/p/pay/tasks/T-1/transition", {"to": "nonsense", "actor": "agent"})
    assert bad.status == 400 and "error" in bad.json()


def test_forbidden_actor_transition_is_403(server):
    add_task(server)
    r = post(server, "/api/p/pay/tasks/T-1/transition", {"to": "done", "actor": "agent"})
    assert r.status == 403


def test_agent_approve_is_403(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/transition", {"to": "approved", "actor": "agent"})
    # assert
    assert r.status == 403


def test_agent_update_of_worktree_is_403(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"worktree": "/tmp/elsewhere"}})
    # assert
    assert r.status == 403
    assert get(server, "/api/p/pay/state").json()["tasks"][0]["worktree"] is None


def test_unknown_task_is_404(server):
    r = post(server, "/api/p/pay/tasks/NOPE/transition", {"to": "in_progress", "actor": "agent"})
    assert r.status == 404


def test_unknown_project_is_404(server):
    assert get(server, "/api/p/ghost/state").status == 404
    assert get(server, "/api/p/..%2Fx/state").status == 404


def test_project_created_after_start_works(server):
    ProjectStore("late").create(ProjectConfig(slug="late", title="Late", chains=True))
    assert get(server, "/api/p/late/state").status == 200


def test_malformed_json_is_400(server):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/p/pay/tasks" % server.port, data=b"{not json", method="POST",
        headers={"X-Board-Token": server.token, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_missing_body_fields_is_400(server):
    assert post(server, "/api/p/pay/tasks", {"actor": "user"}).status == 400
    add_task(server)
    assert post(server, "/api/p/pay/tasks/T-1/transition", {"actor": "agent"}).status == 400


def test_messages_authors(server):
    add_task(server)
    r = post(server, "/api/p/pay/threads/T-1/messages", {"author": "user", "text": "привет"})
    assert r.status == 200
    for author in ("system", "root"):
        bad = post(server, "/api/p/pay/threads/T-1/messages", {"author": author, "text": "x"})
        assert bad.status == 400
    assert post(server, "/api/p/pay/threads/T-1/messages", {"author": "agent", "text": "ok"}).status == 200
    msgs = get(server, "/api/p/pay/threads/T-1").json()["messages"]
    assert [m["text"] for m in msgs] == ["привет", "ok"]
    assert post(server, "/api/p/pay/threads/T-1/read").status == 200


@pytest.mark.parametrize("body", [{"choice": 2, "comment": "ок"}, {"choice": "a", "comment": 5}, {"choice": ["a"]}])
def test_answer__invalid_types__returns_400_and_keeps_question_open(server, body):
    # arrange
    add_task(server)
    post(server, "/api/p/pay/tasks/T-1/questions", {"question": {"text": "Как?", "options": ["a", "b"]}})

    # act
    bad = post(server, "/api/p/pay/tasks/T-1/questions/q1/answer", body)

    # assert
    assert bad.status == 400
    assert post(server, "/api/p/pay/tasks/T-1/questions/q1/answer", {"choice": "a", "comment": ""}).status == 200


def test_questions_update_deps_and_answer(server):
    add_task(server)
    add_task(server, "T-2")
    r = post(server, "/api/p/pay/tasks/T-1/questions", {"question": {"text": "Как?", "options": ["a", "b"]}})
    assert r.status == 200 and r.json()["question_id"] == "q1"
    a = post(server, "/api/p/pay/tasks/T-1/questions/q1/answer", {"choice": "a", "comment": "ок"})
    assert a.status == 200
    assert post(server, "/api/p/pay/tasks/T-1/questions/q9/answer", {"choice": "a", "comment": ""}).status == 404
    u = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"summary": "s"}})
    assert u.status == 200
    assert post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"bogus": 1}}).status == 400
    d = post(server, "/api/p/pay/tasks/T-2/deps", {"deps": ["T-1"], "actor": "user"})
    assert d.status == 200


def test_proposals_apply_and_reject(server):
    r = post(server, "/api/p/pay/proposals",
             {"kind": "add_task", "payload": {"task": {"key": "T-9", "title": "new"}}, "reason": "надо"})
    assert r.status == 200, r.body
    pid = r.json()["proposal_id"]
    ap = post(server, "/api/p/pay/proposals/%s/apply" % pid)
    assert ap.status == 200
    assert [t["key"] for t in get(server, "/api/p/pay/state").json()["tasks"]] == ["T-9"]
    assert post(server, "/api/p/pay/proposals/nope/reject").status == 404


@pytest.mark.parametrize("body", [
    {"kind": "add_task", "payload": {}},
    {"kind": "add_task", "payload": {"task": "x"}},
    {"kind": "set_deps", "payload": {"deps": []}},
    {"kind": "cancel_task", "payload": {}},
    {"kind": "add_task", "payload": "str"},
    {"kind": "add_task"},
])
def test_malformed_proposal_is_400(server, body):
    assert post(server, "/api/p/pay/proposals", body).status == 400


def test_negative_content_length_is_400(server):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        s.sendall(("POST /api/p/pay/tasks HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Board-Token: %s\r\n"
                   "Content-Length: -5\r\n\r\n" % (server.port, server.token)).encode())
        assert " 400 " in read_until(s, "\r\n")
    finally:
        s.close()


def test_events_since(server):
    add_task(server)
    all_events = get(server, "/api/p/pay/events?since=0").json()["events"]
    assert len(all_events) >= 1
    last = all_events[-1]["seq"]
    assert get(server, "/api/p/pay/events?since=%d" % last).json()["events"] == []
    assert get(server, "/api/p/pay/events?since=abc").status == 400


# ---------- SSE ----------

def open_sse(server, path="/api/p/pay/stream", headers=None):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    lines = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % server.port, "X-Board-Token: %s" % server.token]
    for k, v in (headers or {}).items():
        lines.append("%s: %s" % (k, v))
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return s


def read_until(sock, needle, limit=5.0):
    buf = ""
    deadline = time.time() + limit
    while needle not in buf and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk.decode()
    return buf


def test_sse_replays_missed_events_by_last_event_id(server):
    add_task(server, "T-1")   # task_added + agent_wake -> seq 1, 2
    post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"summary": "s"}})  # seq 3
    seqs = [e["seq"] for e in get(server, "/api/p/pay/events?since=0").json()["events"]]
    assert seqs[:3] == [1, 2, 3]
    sock = open_sse(server, headers={"Last-Event-ID": "1"})
    try:
        head = read_until(sock, "id: 3\n")
        assert "text/event-stream" in head and "no-cache" in head.lower()
        assert "id: 1\n" not in head
        assert "id: 2\n" in head and "id: 3\n" in head
        assert "event: " in head and "data: " in head
        post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"summary": "t"}})
        new_seq = get(server, "/api/p/pay/events?since=0").json()["events"][-1]["seq"]
        tail = read_until(sock, "id: %d\n" % new_seq)
        assert "id: %d\n" % new_seq in tail
    finally:
        sock.close()


def test_sse_since_query_param(server):
    add_task(server, "T-1")
    sock = open_sse(server, path="/api/p/pay/stream?since=1")
    try:
        out = read_until(sock, "id: 2\n")
        assert "id: 2\n" in out and "id: 1\n" not in out
    finally:
        sock.close()


def test_stop_returns_promptly_with_open_sse_client(tmp_home):
    ProjectStore("pay").create(ProjectConfig(slug="pay", title="P", chains=True))
    srv = BoardServer(port=0)
    srv.start()
    sock = open_sse(srv)
    read_until(sock, "\r\n\r\n")
    t0 = time.time()
    srv.stop()
    assert time.time() - t0 < 3
    # сервер закрыл поток: клиент доходит до EOF
    assert sock.recv(4096) == b""
    sock.close()


def test_discovery_file_written_and_removed(tmp_home):
    srv = BoardServer(port=0)
    assert read_discovery() is None
    srv.start()
    path = tmp_home / "server.json"
    data = json.loads(path.read_text())
    assert data == {"port": srv.port, "token": srv.token, "pid": os.getpid(), "version": __version__}
    assert (path.stat().st_mode & 0o777) == 0o600
    assert read_discovery() == data
    assert srv.url == "http://127.0.0.1:%d/?token=%s" % (srv.port, srv.token)
    srv.stop()
    assert not path.exists()
    assert read_discovery() is None


def test_discovery_ignores_dead_pid(tmp_home):
    (tmp_home / "server.json").write_text(json.dumps({"port": 1, "token": "t", "pid": 2 ** 22 + 12345}))
    assert read_discovery() is None


# ---------- финальная волна правок ----------

def test_update_with_key_field_is_400(server):
    # arrange
    add_task(server)

    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"key": "x"}})

    # assert
    assert r.status == 400
    assert get(server, "/api/p/pay/state").json()["tasks"][0]["key"] == "T-1"


def test_add_task_with_non_creatable_field_is_400(server):
    # act
    r = add_task(server, status="done")

    # assert
    assert r.status == 400
    assert get(server, "/api/p/pay/state").json()["tasks"] == []


def test_message_with_from_task_escalates_to_project_agent(server):
    # arrange
    add_task(server)

    # act
    r = post(server, "/api/p/pay/threads/_project/messages", {"author": "agent", "text": "помогите", "from_task": "T-1"})

    # assert
    assert r.status == 200
    wakes = [e["data"] for e in r.json()["events"] if e["type"] == "agent_wake"]
    assert wakes == [{"key": "_project", "reason": "escalation", "from": "T-1", "by": "agent"}]


def test_message_with_non_string_from_task_is_400(server):
    # act
    r = post(server, "/api/p/pay/threads/_project/messages", {"author": "agent", "text": "x", "from_task": 5})

    # assert
    assert r.status == 400


def test_sse_uses_larger_of_since_and_last_event_id(server):
    # arrange
    add_task(server, "T-1")   # seq 1, 2
    add_task(server, "T-2")   # seq 3, 4

    # act
    by_header = open_sse(server, path="/api/p/pay/stream?since=1", headers={"Last-Event-ID": "3"})
    by_query = open_sse(server, path="/api/p/pay/stream?since=3", headers={"Last-Event-ID": "1"})
    try:
        out_header = read_until(by_header, "id: 4\n")
        out_query = read_until(by_query, "id: 4\n")
    finally:
        by_header.close()
        by_query.close()

    # assert
    for out in (out_header, out_query):
        assert "id: 4\n" in out
        assert "id: 2\n" not in out and "id: 3\n" not in out


def test_second_start_on_same_home_fails_and_keeps_first_discovery(server):
    # arrange
    first = read_discovery()
    other = BoardServer(port=0)

    # act
    with pytest.raises(ServerAlreadyRunning) as ei:
        other.start()
    other.stop()

    # assert
    assert "уже запущена" in str(ei.value)
    assert read_discovery() == first
    assert get(server, "/api/projects").status == 200


def test_start_refuses_when_discovery_points_to_live_server(tmp_home):
    # arrange
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    (tmp_home / "server.json").write_text(json.dumps(
        {"port": listener.getsockname()[1], "token": "t", "pid": os.getppid()}))
    srv = BoardServer(port=0)

    # act / assert
    try:
        with pytest.raises(ServerAlreadyRunning):
            srv.start()
    finally:
        srv.stop()
        listener.close()
    assert json.loads((tmp_home / "server.json").read_text())["token"] == "t"


def test_stop_keeps_foreign_discovery_file(server, tmp_home):
    # arrange
    foreign = {"port": 1, "token": "чужой", "pid": os.getppid()}
    (tmp_home / "server.json").write_text(json.dumps(foreign))

    # act
    server.stop()

    # assert
    assert json.loads((tmp_home / "server.json").read_text()) == foreign


def test_token_persisted_with_private_mode(tmp_home):
    # arrange
    srv = BoardServer(port=0)

    # act
    srv.start()
    srv.stop()

    # assert
    path = tmp_home / "token"
    assert path.read_text().strip() == srv.token
    assert (path.stat().st_mode & 0o777) == 0o600


def test_restart_keeps_token_and_default_port(tmp_home):
    # arrange
    ProjectStore("pay").create(ProjectConfig(slug="pay", title="P", chains=True))
    first = BoardServer()
    first.start()
    old_port, old_token = first.port, first.token
    first.stop()

    # act
    second = BoardServer()
    second.start()
    try:
        r = get(second, "/api/projects", token=None, headers={"Cookie": "board_token=%s" % old_token})
        new_port = second.port
    finally:
        second.stop()

    # assert
    assert second.token == old_token
    assert r.status == 200
    if old_port not in DEFAULT_PORTS:
        pytest.skip("все порты по умолчанию заняты")
    assert new_port == old_port


def test_busy_default_port_falls_back_to_next(tmp_home, monkeypatch):
    # arrange
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(server_mod, "DEFAULT_PORTS", [busy.getsockname()[1], free])
    srv = BoardServer()

    # act
    try:
        srv.start()
        port = srv.port
    finally:
        srv.stop()
        busy.close()

    # assert
    assert port == free


def test_all_default_ports_busy_falls_back_to_random(tmp_home, monkeypatch):
    # arrange
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]
    monkeypatch.setattr(server_mod, "DEFAULT_PORTS", [busy_port])
    srv = BoardServer()

    # act
    try:
        srv.start()
        port = srv.port
    finally:
        srv.stop()
        busy.close()

    # assert
    assert port > 0 and port != busy_port


def test_default_port_with_foreign_listener_on_all_interfaces_is_skipped(tmp_home):
    # arrange
    foreign = socket.socket()
    foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        foreign.bind(("0.0.0.0", DEFAULT_PORTS[0]))
    except OSError:
        foreign.close()
        pytest.skip("первый порт по умолчанию недоступен")
    foreign.listen(1)
    srv = BoardServer()

    # act
    try:
        srv.start()
        port = srv.port
    finally:
        srv.stop()
        foreign.close()

    # assert
    assert port != DEFAULT_PORTS[0]


def test_start_failure_after_bind_releases_lock_and_socket(tmp_home, monkeypatch):
    # arrange
    srv = BoardServer(port=0)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    # act
    with monkeypatch.context() as m:
        m.setattr(server_mod, "atomic_write_json", boom)
        with pytest.raises(RuntimeError):
            srv.start()
    again = BoardServer(port=0)
    again.start()
    again.stop()

    # assert
    assert srv._httpd is None
    assert srv._lock_fd is None
    assert srv.port == 0
    assert again.port > 0
    assert not (tmp_home / "server.json").exists()


def test_start_failure_on_thread_start_does_not_hang_and_allows_retry(tmp_home, monkeypatch):
    # arrange
    srv = BoardServer(port=0)

    def boom(self):
        raise RuntimeError("can't start new thread")

    # act
    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            srv.start()
    again = BoardServer(port=0)
    again.start()
    again.stop()

    # assert
    assert srv._httpd is None
    assert srv._thread is None
    assert srv._lock_fd is None
    assert again.port > 0


def test_existing_token_with_loose_mode_is_tightened(tmp_home):
    # arrange
    tmp_home.mkdir(parents=True, exist_ok=True)
    path = tmp_home / "token"
    path.write_text("sometoken\n")
    os.chmod(str(path), 0o644)
    srv = BoardServer(port=0)

    # act
    srv.start()
    srv.stop()

    # assert
    assert srv.token == "sometoken"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_sse_reconnect_after_restart_replays_missed_events(tmp_home):
    # arrange
    ProjectStore("pay").create(ProjectConfig(slug="pay", title="P", chains=True))
    first = BoardServer(port=0)
    first.start()
    old_token = first.token
    add_task(first, "T-1")   # seq 1, 2 — клиент их видел
    first.stop()
    second = BoardServer(port=0)
    second.start()
    try:
        add_task(second, "T-2")   # seq 3, 4 — пропущены за время рестарта

        # act
        sock = open_sse(second, headers={"Last-Event-ID": "2"})
        try:
            out = read_until(sock, "id: 4\n")
        finally:
            sock.close()
    finally:
        second.stop()

    # assert
    assert second.token == old_token
    assert "id: 3\n" in out and "id: 4\n" in out
    assert "id: 2\n" not in out


def test_stage_change_by_user_and_errors(server):
    # act
    ok = post(server, "/api/p/pay/stage", {"stage": "work", "actor": "user"})
    bad = post(server, "/api/p/pay/stage", {"stage": "decomposition", "actor": "user"})
    agent = post(server, "/api/p/pay/stage", {"stage": "done", "actor": "agent"})
    missing = post(server, "/api/p/pay/stage", {"actor": "user"})
    # assert
    assert ok.status == 200
    assert [(e["type"], e["data"]) for e in ok.json()["events"]] == [
        ("stage_changed", {"from": "decomposition", "to": "work"})]
    assert bad.status == 400 and agent.status == 403 and missing.status == 400
    assert get(server, "/api/p/pay/state").json()["config"]["stage"] == "work"


def test_stage_without_actor_is_rejected(server):
    # act
    r = post(server, "/api/p/pay/stage", {"stage": "work"})
    # assert
    assert r.status == 400 and "actor" in r.json()["error"]
    assert get(server, "/api/p/pay/state").json()["config"]["stage"] == "decomposition"


# --- API для интерфейса ---

@pytest.fixture
def ui_server(tmp_home, git_repo):
    from agent_board.worktree import ensure_worktree

    s = ProjectStore("pay")
    s.create(ProjectConfig(slug="pay", title="Платежи", repos=[{"name": "app", "path": str(git_repo), "base": "main"}]))
    wt = ensure_worktree(git_repo, "T-1", "T-1", "main", [])
    s.save_task(make_task("T-1", repo="app", worktree=str(wt), base_branch="main"))
    s.save_task(make_task("T-2"))
    srv = BoardServer(port=0)
    srv.start()
    srv.worktree = wt
    srv.repo = git_repo
    yield srv
    srv.stop()


def test_post_tasks_returns_key(server):
    # act
    r = add_task(server, "T-7")
    # assert
    assert r.json()["key"] == "T-7"


def test_diff_returns_changed_files_and_text(ui_server):
    # arrange
    (ui_server.worktree / "new.txt").write_text("hello\n")
    (ui_server.worktree / "a.txt").write_text("changed\n")
    # act
    r = get(ui_server, "/api/p/pay/tasks/T-1/diff")
    # assert
    body = r.json()
    assert r.status == 200 and body["base"] == "main" and body["truncated"] is False
    assert body["files"] == ["a.txt", "new.txt"]
    assert "+changed" in body["diff"] and "+hello" in body["diff"]


def test_diff_without_worktree_is_empty(ui_server):
    # act
    r = get(ui_server, "/api/p/pay/tasks/T-2/diff")
    # assert
    assert r.json() == {"files": [], "diff": "", "base": None, "truncated": False}


def test_diff_unknown_task_is_404(ui_server):
    assert get(ui_server, "/api/p/pay/tasks/T-404/diff").status == 404


def test_diff_ignores_foreign_worktree_path(ui_server, tmp_path):
    # arrange: путь задачи указывает не на её worktree
    store = ProjectStore("pay")
    t = store.load_tasks()["T-1"]
    t.worktree = str(tmp_path)
    store.save_task(t)
    # act
    r = get(ui_server, "/api/p/pay/tasks/T-1/diff")
    # assert
    assert r.json() == {"files": [], "diff": "", "base": None, "truncated": False}


def test_diff_truncated_on_line_boundary(ui_server):
    # arrange
    line = "x" * 99 + "\n"
    (ui_server.worktree / "big.txt").write_text(line * 6000)
    # act
    body = get(ui_server, "/api/p/pay/tasks/T-1/diff").json()
    # assert
    assert body["truncated"] is True
    assert len(body["diff"].encode()) <= server_mod.MAX_DIFF_BYTES
    assert body["diff"].endswith("\n") and body["diff"].splitlines()[-1] == "+" + "x" * 99


def test_file_serves_project_file_as_plain_text(ui_server):
    # arrange
    store = ProjectStore("pay")
    (store.root / "notes.md").write_text("# привет\n", encoding="utf-8")
    # act
    r = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(store.root / "notes.md")))
    # assert
    assert r.status == 200 and r.body.decode() == "# привет\n"
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"


def test_file_relative_path_resolves_against_first_repo(ui_server):
    # arrange
    (ui_server.repo / "docs").mkdir()
    (ui_server.repo / "docs" / "plan.md").write_text("план", encoding="utf-8")
    (ProjectStore("pay").root / "docs").mkdir()
    (ProjectStore("pay").root / "docs" / "plan.md").write_text("не тот", encoding="utf-8")
    # act
    r = get(ui_server, "/api/p/pay/file?path=docs/plan.md")
    # assert
    assert r.status == 200 and r.body.decode() == "план"


def test_file_relative_path_without_repos_resolves_against_project_dir(server):
    # arrange
    (ProjectStore("pay").root / "notes.md").write_text("заметки", encoding="utf-8")
    # act
    r = get(server, "/api/p/pay/file?path=notes.md")
    # assert
    assert r.status == 200 and r.body.decode() == "заметки"


def test_file_html_served_with_sandbox_csp(ui_server):
    # arrange
    path = ui_server.worktree / "page.html"
    path.write_text("<h1>x</h1>")
    # act
    r = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(path)))
    # assert
    assert r.status == 200 and r.headers["Content-Type"] == "text/html; charset=utf-8"
    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_file_outside_allowed_dirs_is_forbidden(ui_server, tmp_path):
    # arrange
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    store = ProjectStore("pay")
    (store.root / "link.txt").symlink_to(secret)
    # act
    absolute = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(secret)))
    traversal = get(ui_server, "/api/p/pay/file?path=..%2F..%2F..%2Fsecret.txt")
    symlink = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(store.root / "link.txt")))
    missing_outside = get(ui_server, "/api/p/pay/file?path=%2Fnope%2Fnot-here")
    # assert
    assert absolute.status == traversal.status == symlink.status == missing_outside.status == 403
    assert b"secret" not in absolute.body + traversal.body + symlink.body


def test_file_errors(ui_server):
    # arrange
    store = ProjectStore("pay")
    (store.root / "big.txt").write_bytes(b"x" * (server_mod.MAX_FILE_BYTES + 1))
    # assert
    assert get(ui_server, "/api/p/pay/file").status == 400
    assert get(ui_server, "/api/p/pay/file?path=absent.txt").status == 404
    assert get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(store.root / "big.txt"))).status == 413


def test_update_rule_violations_by_user_and_project_agent_only(server):
    # arrange
    add_task(server)
    value = [{"rule": "Не коммитить", "text": "закоммитил"}]
    # act
    by_user = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"rule_violations": value}})
    by_pa = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "project_agent", "fields": {"rule_violations": []}})
    by_agent = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"rule_violations": value}})
    # assert
    assert by_user.status == 200 and by_pa.status == 200 and by_agent.status == 403


@pytest.mark.parametrize("bad", ["x", [1], [{"rule": "a"}], [{"rule": 1, "text": "b"}], [{"rule": "a", "text": "b"}] * 51])
def test_update_rule_violations_validated(server, bad):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"rule_violations": bad}})
    # assert
    assert r.status == 400


def test_update_rule_violations_strips_unknown_keys_and_caps_length(server):
    # arrange
    add_task(server)
    value = [{"rule": "r" * 300, "text": "t" * 3000, "extra": "x", "html": "<b>"}]
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"rule_violations": value}})
    # assert
    assert r.status == 200
    stored = get(server, "/api/p/pay/state").json()["tasks"][0]["rule_violations"]
    assert stored == [{"rule": "r" * 200, "text": "t" * 2000}]


def test_state_exposes_links_and_rule_violations(server):
    # arrange
    add_task(server)
    value = [{"rule": "r", "text": "t"}]
    post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"rule_violations": value}})
    # act
    state = get(server, "/api/p/pay/state").json()
    # assert
    assert state["config"]["links"] == []
    assert state["tasks"][0]["rule_violations"] == value


# --- fix round 1 ---

def test_file_does_not_trust_self_declared_worktree(ui_server, tmp_path):
    # arrange
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "key.txt").write_text("private")
    store = ProjectStore("pay")
    t = store.load_tasks()["T-2"]
    t.worktree = str(outside)
    store.save_task(t)
    # act
    r = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(outside / "key.txt")))
    # assert
    assert r.status == 403


def test_file_denies_git_dir_and_logs(ui_server):
    # arrange
    store = ProjectStore("pay")
    (store.root / "logs").mkdir()
    (store.root / "logs" / "T-1.jsonl").write_text("{}")
    # act
    git_config = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(ui_server.repo / ".git" / "config")))
    wt_git = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(ui_server.worktree / ".git")))
    logs = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(store.root / "logs" / "T-1.jsonl")))
    # assert
    assert git_config.status == wt_git.status == logs.status == 403


def test_file_html_content_type_has_charset(ui_server):
    # arrange
    (ui_server.worktree / "p.html").write_text("<p>привет</p>", encoding="utf-8")
    # act
    r = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(ui_server.worktree / "p.html")))
    # assert
    assert r.headers["Content-Type"] == "text/html; charset=utf-8"


def test_diff_without_newlines_is_cut_by_bytes_not_emptied(ui_server):
    # arrange
    (ui_server.worktree / "one.txt").write_text("я" * (server_mod.MAX_DIFF_BYTES))
    # act
    body = get(ui_server, "/api/p/pay/tasks/T-1/diff").json()
    # assert
    assert body["truncated"] is True and body["diff"] != ""
    assert len(body["diff"].encode()) <= server_mod.MAX_DIFF_BYTES


def test_empty_diff_reports_truncated_false(ui_server):
    assert get(ui_server, "/api/p/pay/tasks/T-2/diff").json() == {"files": [], "diff": "", "base": None, "truncated": False}


def test_diff_cut_by_bytes_when_no_line_break_fits(ui_server, monkeypatch):
    # arrange
    (ui_server.worktree / "one.txt").write_text("x\n")
    monkeypatch.setattr(server_mod, "MAX_DIFF_BYTES", 10)
    # act
    body = get(ui_server, "/api/p/pay/tasks/T-1/diff").json()
    # assert
    assert body["truncated"] is True and body["diff"] == "diff --git"


# --- статика интерфейса ---

APP_CSP = ("default-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'")


def test_root_serves_packaged_index_with_csp(server):
    # act
    r = get(server, "/", token=None)
    # assert
    assert r.status == 200 and r.headers["Content-Type"] == "text/html; charset=utf-8"
    assert '<script type="module" src="/static/js/main.js"></script>' in r.body.decode()
    assert r.headers["Content-Security-Policy"] == APP_CSP
    assert r.headers["Cache-Control"] == "no-cache"
    assert "Set-Cookie" not in r.headers


def test_root_with_wrong_token_sets_no_cookie(server):
    # act
    r = get(server, "/?token=wrong", token=None)
    # assert
    assert r.status == 200 and "Set-Cookie" not in r.headers


@pytest.mark.parametrize("path, ctype", [
    ("/static/js/main.js", "text/javascript; charset=utf-8"),
    ("/static/app.css", "text/css; charset=utf-8"),
])
def test_static_assets_have_mime_and_no_cache(server, path, ctype):
    # act
    r = get(server, path, token=None)
    # assert
    assert r.status == 200 and r.headers["Content-Type"] == ctype
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Security-Policy"] == APP_CSP


def test_static_svg_mime(tmp_home, tmp_path):
    # arrange
    (tmp_path / "logo.svg").write_text("<svg/>")
    srv = BoardServer(port=0, static_dir=tmp_path)
    srv.start()
    try:
        # act
        r = get(srv, "/static/logo.svg", token=None)
    finally:
        srv.stop()
    # assert
    assert r.status == 200 and r.headers["Content-Type"] == "image/svg+xml"


def test_unknown_page_and_missing_static_are_404(server):
    # assert
    assert get(server, "/index.html", token=None).status == 404
    assert get(server, "/static/js/nope.js", token=None).status == 404
    assert get(server, "/static/", token=None).status == 404


def test_file_denies_git_dir_and_logs_case_insensitive(ui_server):
    # arrange
    store = ProjectStore("pay")
    (store.root / "logs").mkdir()
    (store.root / "logs" / "T-1.jsonl").write_text("{}")
    # act
    git_config = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(ui_server.repo / ".GIT" / "config")))
    logs = get(ui_server, "/api/p/pay/file?path=" + urllib.parse.quote(str(store.root / "LOGS" / "T-1.jsonl")))
    # assert
    assert git_config.status == logs.status == 403
    assert b"[core]" not in git_config.body


def test_store_js_listens_to_every_event_type():
    # arrange
    from agent_board.board import EVENT_TYPES

    text = (Path(server_mod.__file__).parent / "static" / "js" / "store.js").read_text(encoding="utf-8")
    block = text[text.index("export const EVENT_TYPES"):]
    block = block[:block.index("];")]
    # act
    listed = set(re.findall(r"'([a-z_]+)'", block))
    # assert
    assert listed == set(EVENT_TYPES)


# --- ревью ---

def to_review(server, key="T-1"):
    add_task(server, key)
    post(server, "/api/p/pay/tasks/%s/transition" % key, {"to": "in_review", "actor": "agent"})


def task_state(server, key="T-1"):
    return next(t for t in get(server, "/api/p/pay/state").json()["tasks"] if t["key"] == key)


def test_review_request_records_event_for_in_review_task(server):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/review", {"actor": "user"})
    # assert
    assert r.status == 200
    assert [e["type"] for e in r.json()["events"]] == ["review_requested"]
    assert r.json()["events"][0]["data"] == {"key": "T-1"}


def test_review_request_outside_in_review_is_400(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/review", {"actor": "user"})
    # assert
    assert r.status == 400 and "ревью" in r.json()["error"]


def test_review_request_by_agent_is_403(server):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/review", {"actor": "agent"})
    # assert
    assert r.status == 403


def test_review_request_with_open_round_is_400(server):
    # arrange
    to_review(server)
    server.board_for("pay").update_task("T-1", "system", {"review": {"round": 1, "state": "open"}})
    # act
    r = post(server, "/api/p/pay/tasks/T-1/review", {"actor": "user"})
    # assert
    assert r.status == 400


def test_review_request_without_review_tool_is_400(server):
    # arrange
    server.board_for("pay").update_config({"review": {"tool": "none"}})
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/review", {"actor": "user"})
    # assert
    assert r.status == 400


def test_approve_by_user_moves_to_approved(server):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/approve", {"actor": "user"})
    # assert
    assert r.status == 200
    changed = [e["data"] for e in r.json()["events"] if e["type"] == "status_changed"]
    assert changed == [{"key": "T-1", "from": "in_review", "to": "approved", "actor": "user"}]
    assert task_state(server)["status"] == "approved"


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system"])
def test_approve_by_non_user_is_403(server, actor):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/approve", {"actor": actor})
    # assert
    assert r.status == 403 and task_state(server)["status"] == "in_review"


def test_approve_outside_in_review_is_400(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/approve", {"actor": "user"})
    # assert
    assert r.status == 400


def test_return_with_comment_posts_message_and_wakes_once_with_it(server):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/return", {"actor": "user", "text": "поправь тесты"})
    # assert
    assert r.status == 200
    evs = r.json()["events"]
    assert [e["type"] for e in evs] == ["message_posted", "status_changed", "agent_wake"]
    wake = evs[2]["data"]
    assert wake["reason"] == "returned" and wake["from"] == "in_review"
    assert wake["message_id"] == evs[0]["data"]["message_id"]
    assert task_state(server)["status"] == "in_progress"
    thread = get(server, "/api/p/pay/threads/T-1").json()["messages"]
    assert [(m["author"], m["text"]) for m in thread] == [("user", "поправь тесты")]


@pytest.mark.parametrize("body,status", [
    ({"actor": "user", "text": "  "}, 400),
    ({"actor": "user"}, 400),
    ({"actor": "agent", "text": "x"}, 403),
])
def test_return_validation(server, body, status):
    # arrange
    to_review(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/return", body)
    # assert
    assert r.status == status and task_state(server)["status"] == "in_review"


def test_return_from_in_progress_is_400(server):
    # arrange
    add_task(server)
    post(server, "/api/p/pay/tasks/T-1/transition", {"to": "in_progress", "actor": "agent"})
    # act
    r = post(server, "/api/p/pay/tasks/T-1/return", {"actor": "user", "text": "x"})
    # assert
    assert r.status == 400


def test_review_field_is_runner_only(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"review": {"state": "approved"}}})
    # assert
    assert r.status == 403 and task_state(server)["review"] is None


# --- отправка в ревью ---

def approved_task(server, key="T-1", **extra):
    add_task(server, key, **extra)
    post(server, "/api/p/pay/tasks/%s/transition" % key, {"to": "in_review", "actor": "agent"})
    post(server, "/api/p/pay/tasks/%s/approve" % key, {"actor": "user"})


def with_ship(server, **ship_cfg):
    server.board_for("pay").update_config({"ship": ship_cfg})


def test_ship_request_queues_tasks_in_dependency_order(server):
    # arrange
    with_ship(server, command="true")
    approved_task(server, "A")
    approved_task(server, "B", deps=["A"])
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["B", "A"], "actor": "user"})
    # assert
    assert r.status == 200
    assert [e["data"] for e in r.json()["events"] if e["type"] == "ship_requested"] == [{"keys": ["A", "B"]}]
    assert task_state(server, "A")["ship"]["state"] == "queued"


def test_ship_without_command_is_400_with_reason(server):
    # arrange
    approved_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # assert
    assert r.status == 400 and "команда отправки" in r.json()["error"]


def test_ship_unknown_placeholder_is_400_at_request_time(server):
    # arrange
    server.board_for("pay").store.save_config(
        _with_ship(server.board_for("pay").store.load_config(), "echo {nope}"))
    approved_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # assert
    assert r.status == 400 and "{nope}" in r.json()["error"] and task_state(server)["ship"] is None


def test_ship_task_not_approved_is_400(server):
    # arrange
    with_ship(server, command="true")
    add_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # assert
    assert r.status == 400 and task_state(server)["ship"] is None


def test_ship_with_unshipped_dependency_outside_request_is_400(server):
    # arrange
    with_ship(server, command="true")
    approved_task(server, "A")
    approved_task(server, "B", deps=["A"])
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["B"], "actor": "user"})
    # assert
    assert r.status == 400 and "сначала отправьте A" in r.json()["error"]


def test_ship_second_request_while_running_is_400(server):
    # arrange
    with_ship(server, command="true")
    approved_task(server)
    first = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # act
    second = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # assert
    assert first.status == 200 and second.status == 400 and "уже отправляется" in second.json()["error"]


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system"])
def test_ship_by_non_user_is_403(server, actor):
    # arrange
    with_ship(server, command="true")
    approved_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": actor})
    # assert
    assert r.status == 403 and task_state(server)["ship"] is None


@pytest.mark.parametrize("keys", [[], "T-1", ["T-1", "T-1"], [1]])
def test_ship_bad_keys_is_400(server, keys):
    # arrange
    with_ship(server, command="true")
    approved_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": keys, "actor": "user"})
    # assert
    assert r.status == 400


def test_ship_unknown_task_is_404(server):
    # arrange
    with_ship(server, command="true")
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["NOPE"], "actor": "user"})
    # assert
    assert r.status == 404


def test_agent_cannot_write_ship_field(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"ship": {"state": "done"}}})
    # assert
    assert r.status == 403


def test_ship_quoted_placeholder_is_400_at_request_time(server):
    # arrange
    server.board_for("pay").store.save_config(
        _with_ship(server.board_for("pay").store.load_config(), 'run "{title}"'))
    approved_task(server)
    # act
    r = post(server, "/api/p/pay/ship", {"keys": ["T-1"], "actor": "user"})
    # assert
    assert r.status == 400 and "в кавычки" in r.json()["error"]


def test_config_update_with_quoted_placeholder_is_rejected(server):
    # arrange, act, assert
    with pytest.raises(ValueError, match="в кавычки"):
        with_ship(server, command="run '{title}'")


def _with_ship(config, command):
    config.ship = {"command": command, "timeout": 5}
    return config


# --- трекер ---

TRACKER_TOKEN = "trk-S3CRET-value-42"


@pytest.fixture
def jira(monkeypatch):
    from fakes.fake_jira import FakeJira

    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIRA_TOKEN", TRACKER_TOKEN)
    fake = FakeJira(authorization="Bearer " + TRACKER_TOKEN).start()
    yield fake
    fake.stop()


def with_jira(server, jira, stage="tracker"):
    board = server.board_for("pay")
    board.update_config({"tracker": {"type": "jira", "url": jira.url, "project": "PROJ", "issue_type": "Task",
                                     "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}})
    add_task(server, "A")
    add_task(server, "B", deps=["A"])
    if stage == "tracker":
        board.set_stage("tracker", "user")


def test_tracker_create_creates_issues_and_links(server, jira):
    # arrange
    with_jira(server, jira)
    # act
    r = post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    # assert
    assert r.status == 200
    body = r.json()
    assert body["errors"] == [] and [c["tracker_key"] for c in body["created"]] == ["PROJ-1", "PROJ-2"]
    assert body["linked"] == [["A", "B"]]
    tasks = {t["key"]: t for t in get(server, "/api/p/pay/state").json()["tasks"]}
    assert tasks["B"]["tracker_url"] == jira.url + "/browse/PROJ-2"


def test_tracker_create_401_never_leaks_token(server, jira, capfd):
    # arrange: трекер отвечает 401 и повторяет токен в тексте ошибки
    with_jira(server, jira)
    jira.fail_all = (401, {"errorMessages": ["token %s rejected" % TRACKER_TOKEN]})
    # act
    r = post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    check = post(server, "/api/p/pay/tracker/check", {"actor": "user"})
    # assert
    assert r.status == 200 and "авторизац" in r.json()["errors"][0]["error"]
    assert check.json()["ok"] is False and check.json()["kind"] == "auth"
    seen = [r.body.decode(), check.body.decode(), get(server, "/api/p/pay/events").body.decode(),
            get(server, "/api/p/pay/state").body.decode(), "".join(capfd.readouterr())]
    home = Path(os.environ["AGENT_BOARD_HOME"])
    seen += [p.read_bytes().decode("utf-8", "replace") for p in home.rglob("*") if p.is_file()]
    assert all(TRACKER_TOKEN not in text for text in seen)


def test_tracker_create_success_never_leaks_token(server, jira, capfd):
    # arrange
    with_jira(server, jira)
    # act
    r = post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    # assert
    assert r.json()["errors"] == []
    home = Path(os.environ["AGENT_BOARD_HOME"])
    seen = [r.body.decode(), get(server, "/api/p/pay/events").body.decode(), "".join(capfd.readouterr())]
    seen += [p.read_bytes().decode("utf-8", "replace") for p in home.rglob("*") if p.is_file()]
    assert all(TRACKER_TOKEN not in text for text in seen)


def test_tracker_create_outside_tracker_stage_is_400(server, jira):
    # arrange
    with_jira(server, jira, stage="decomposition")
    # act
    r = post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    # assert
    assert r.status == 400 and "tracker" in r.json()["error"] and jira.requests == []


@pytest.mark.parametrize("route", ["create", "check"])
def test_tracker_actions_by_agent_are_403(server, jira, route):
    # arrange
    with_jira(server, jira)
    # act
    r = post(server, "/api/p/pay/tracker/" + route, {"actor": "agent"})
    # assert
    assert r.status == 403 and jira.requests == []


def test_tracker_check_ok(server, jira):
    # arrange
    with_jira(server, jira)
    # act
    r = post(server, "/api/p/pay/tracker/check", {"actor": "user"})
    # assert
    assert r.status == 200 and r.json() == {"ok": True}


def test_tracker_create_missing_env_is_400_naming_variable(server, jira, monkeypatch):
    # arrange
    with_jira(server, jira)
    monkeypatch.delenv("JIRA_TOKEN")
    # act
    r = post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    # assert
    assert r.status == 400 and "JIRA_TOKEN" in r.json()["error"]


def test_update__tracker_link_cleared_by_user__is_removed(server):
    # arrange
    add_task(server)
    link = {"tracker_key": "PROJ-9", "tracker_url": "https://tracker.example/PROJ-9"}
    assert post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": link}).status == 200
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update",
             {"actor": "user", "fields": {"tracker_key": None, "tracker_url": None}})
    # assert
    assert r.status == 200
    task = get(server, "/api/p/pay/state").json()["tasks"][0]
    assert (task["tracker_key"], task["tracker_url"]) == (None, None)


def test_update__tracker_key_wrong_format__bad_request_with_example(server):
    # arrange
    server.board_for("pay").update_config({"tracker": JIRA_OK})
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"tracker_key": "proj-9"}})
    # assert
    assert r.status == 400 and "PROJ-123" in r.json()["error"]
    assert get(server, "/api/p/pay/state").json()["tasks"][0]["tracker_key"] is None


def test_update__tracker_link_cleared_by_agent__forbidden(server):
    # arrange
    add_task(server)
    post(server, "/api/p/pay/tasks/T-1/update", {"actor": "user", "fields": {"tracker_key": "PROJ-9"}})
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"tracker_key": None}})
    # assert
    assert r.status == 403
    assert get(server, "/api/p/pay/state").json()["tasks"][0]["tracker_key"] == "PROJ-9"


def test_agent_cannot_write_tracker_key(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"tracker_key": "PROJ-9"}})
    # assert
    assert r.status == 403


# --- синхронизация статусов трекера ---

from agent_board.tracker import TrackerSync  # noqa: E402

JIRA_MAP = {"Open": None, "In Progress": "in_progress", "Reopened": "in_progress", "Done": "done"}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def sync_project(server, jira, clock, statuses=None, poll=30, stage="work"):
    """Проект с задачами A, B(A), заведёнными в поддельной Jira; синхронизация с поддельными часами."""
    with_jira(server, jira)
    assert post(server, "/api/p/pay/tracker/create", {"actor": "user"}).status == 200
    board = server.board_for("pay")
    tracker_cfg = dict(board.store.load_config().tracker, statuses=JIRA_MAP if statuses is None else statuses,
                       poll_seconds=poll)
    board.update_config({"tracker": tracker_cfg})
    if stage != "tracker":
        board.set_stage("work", "user")
        if stage == "done":
            board.set_stage("done", "user")
    server.tracker_sync = TrackerSync(server, clock=clock)
    return server.tracker_sync


def sync_state(server):
    return get(server, "/api/p/pay/state").json()["tracker_sync"]


def task_status(server, key):
    return next(t["status"] for t in get(server, "/api/p/pay/state").json()["tasks"] if t["key"] == key)


def events_of(server, type):
    return [e for e in get(server, "/api/p/pay/events").json()["events"] if e["type"] == type]


def test_sync__tracker_done__marks_done_and_unblocks_dependent(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "done"
    changes = [e["data"] for e in events_of(server, "status_changed") if e["data"]["actor"] == "tracker"]
    assert changes == [{"key": "A", "from": "not_started", "to": "done", "actor": "tracker"}]
    assert [e["data"]["key"] for e in events_of(server, "task_unblocked")] == ["B"]
    assert sync_state(server)["changed"] == ["A"]


def test_sync__reopened_in_tracker__wakes_agent(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    sync.tick()
    jira.statuses["PROJ-1"] = "Reopened"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "in_progress"
    wakes = [e["data"] for e in events_of(server, "agent_wake") if e["data"].get("reason") == "returned"]
    assert wakes and wakes[-1]["key"] == "A" and wakes[-1]["from"] == "done"


def test_sync__interval_is_respected(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, poll=30)
    sync.tick()
    after_first = len(jira.requests)
    # act
    clock.now = 29
    sync.tick()
    unchanged = len(jira.requests)
    clock.now = 30
    sync.tick()
    # assert
    assert after_first > 0 and unchanged == after_first and len(jira.requests) > after_first


def test_sync__default_interval_is_30_seconds(server, jira, clock):
    # arrange
    with_jira(server, jira)
    post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    server.board_for("pay").set_stage("work", "user")
    sync = server.tracker_sync = TrackerSync(server, clock=clock)
    sync.tick()
    count = len(jira.requests)
    # act
    clock.now = 29
    sync.tick()
    clock.now = 30
    sync.tick()
    # assert
    assert len(jira.requests) > count and sync.next_due("pay") == 60


def test_sync__not_work_stage_or_no_tracker__no_requests(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, stage="tracker")
    before = len(jira.requests)
    # act
    sync.tick()
    # assert
    assert len(jira.requests) == before and sync_state(server) is None


def test_sync__done_stage__still_syncs(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, stage="done")
    before = len(jira.requests)
    # act
    sync.tick()
    # assert
    assert len(jira.requests) > before and sync_state(server)["ok"] is True


def test_sync__unchanged_ticks__emit_no_events(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    count = len(events_of(server, "tracker_synced"))
    # act
    for _ in range(3):
        clock.now += 30
        sync.tick()
    # assert
    assert count == 1 and len(events_of(server, "tracker_synced")) == 1


def test_sync__event_carries_ok_changed_error_only(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert events_of(server, "tracker_synced")[-1]["data"] == {"ok": True, "changed": ["A"], "error": None}


def test_sync__mapping_is_case_insensitive(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, statuses={"in progress": "in_progress", "DONE": "done"})
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "IN PROGRESS"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "done" and task_status(server, "B") == "in_progress"
    assert sync_state(server)["unknown_statuses"] == []


def test_sync__empty_mapping__only_reads_and_lists_unknown_statuses(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, statuses={})
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "not_started"
    assert sync_state(server)["unknown_statuses"] == ["Done", "Open"] and sync_state(server)["changed"] == []
    assert events_of(server, "status_changed") == [] or all(
        e["data"]["actor"] != "tracker" for e in events_of(server, "status_changed"))


def test_sync__unmapped_status__is_listed_and_ignored(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.statuses["PROJ-1"] = "Code Review"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "not_started" and sync_state(server)["unknown_statuses"] == ["Code Review"]


def test_sync__board_status_not_reverted_while_tracker_unchanged(server, jira, clock):
    # arrange: в трекере «In Progress», а доска ушла дальше — это не повод возвращать задачу
    sync = sync_project(server, jira, clock)
    jira.statuses["PROJ-1"] = "In Progress"
    sync.tick()
    board = server.board_for("pay")
    board.transition("A", Status.IN_REVIEW, "agent")
    # act
    clock.now += 30
    sync.tick()
    # assert
    assert task_status(server, "A") == "in_review"


def test_sync__task_transition_error__does_not_stop_others(server, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(server, jira, clock, statuses={"Open": None, "Done": "done"})
    sync.tick()
    board = server.board_for("pay")
    real = board.transition

    def flaky(key, to, actor, *a, **kw):
        if key == "A":
            raise ValueError("нельзя")
        return real(key, to, actor, *a, **kw)

    monkeypatch.setattr(board, "transition", flaky)
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(server)
    assert task_status(server, "B") == "done" and task_status(server, "A") == "not_started"
    assert state["changed"] == ["B"] and [e["key"] for e in state["errors"]] == ["A"]


def test_sync__401__pauses_without_further_requests(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    jira.fail_all = (401, None)
    # act
    sync.tick()
    count = len(jira.requests)
    for _ in range(3):
        clock.now += 30
        sync.tick()
    # assert
    state = sync_state(server)
    assert len(jira.requests) == count and state["paused"] is True and state["ok"] is False
    assert "JIRA_TOKEN" in state["error"]


def test_sync__manual_retry__runs_now_and_unpauses_on_success(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    jira.fail_all = (401, None)
    sync.tick()
    # act
    forbidden = post(server, "/api/p/pay/tracker/sync", {"actor": "agent"})
    still = post(server, "/api/p/pay/tracker/sync", {"actor": "user"})
    jira.fail_all = None
    fixed = post(server, "/api/p/pay/tracker/sync", {"actor": "user"})
    # assert
    assert forbidden.status == 403
    assert still.status == 200 and still.json()["paused"] is True and still.json()["ok"] is False
    assert fixed.json()["ok"] is True and fixed.json()["paused"] is False
    assert sync_state(server)["paused"] is False
    clock.now += 30
    before = len(jira.requests)
    sync.tick()
    assert len(jira.requests) > before


def test_sync__manual_without_tracker_or_wrong_stage__is_400(server, jira, clock):
    # arrange
    sync_project(server, jira, clock, stage="tracker")
    # act
    wrong_stage = post(server, "/api/p/pay/tracker/sync", {"actor": "user"})
    # assert
    assert wrong_stage.status == 400 and "work" in wrong_stage.json()["error"]


def test_sync__tracker_config_change__unpauses(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    jira.fail_all = (401, None)
    sync.tick()
    jira.fail_all = None
    board = server.board_for("pay")
    # act
    board.update_config({"tracker": dict(board.store.load_config().tracker, labels=["x"])})
    sync.tick()
    # assert
    assert sync_state(server)["paused"] is False and sync_state(server)["ok"] is True


def test_sync__paused_state_survives_restart_until_config_changes(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    jira.fail_all = (403, None)
    sync.tick()
    count = len(jira.requests)
    # act: новый экземпляр (рестарт сервера) читает паузу из проекта
    server.tracker_sync = TrackerSync(server, clock=clock)
    server.tracker_sync.tick()
    # assert
    assert len(jira.requests) == count and sync_state(server)["paused"] is True


def test_sync__server_errors__back_off_doubling_up_to_5_minutes(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, poll=30)
    jira.fail_all = (503, {"errorMessages": ["down"]})
    delays = []
    # act
    for _ in range(7):
        sync.tick()
        delays.append(sync.next_due("pay") - clock.now)
        clock.now = sync.next_due("pay")
    # assert
    assert delays == [30, 60, 120, 240, 300, 300, 300]
    assert sync_state(server)["paused"] is False and sync_state(server)["ok"] is False


def test_sync__recovery_after_network_error__resets_interval(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, poll=30)
    jira.fail_all = (503, None)
    sync.tick()
    clock.now = sync.next_due("pay")
    sync.tick()
    jira.fail_all = None
    clock.now = sync.next_due("pay")
    # act
    sync.tick()
    # assert
    assert sync_state(server)["ok"] is True and sync.next_due("pay") == clock.now + 30


def test_sync__errors_emit_event_once_per_change(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, poll=30)
    sync.tick()
    jira.fail_all = (503, None)
    # act
    for _ in range(3):
        clock.now = sync.next_due("pay")
        sync.tick()
    # assert
    synced = events_of(server, "tracker_synced")
    assert [e["data"]["ok"] for e in synced] == [True, False]


def test_sync__secrets_never_reach_state_events_or_files(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    jira.fail_all = (400, {"errorMessages": ["bad token " + TRACKER_TOKEN]})
    # act
    sync.tick()
    clock.now += 30
    jira.fail_all = (401, {"errorMessages": [TRACKER_TOKEN]})
    sync.tick()
    # assert
    board = server.board_for("pay")
    everything = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                           for p in board.store.root.rglob("*") if p.is_file())
    everything += get(server, "/api/p/pay/state").body.decode() + get(server, "/api/p/pay/events").body.decode()
    assert TRACKER_TOKEN not in everything and "tracker_sync" in everything


def test_sync__github_closed__marks_done(server, monkeypatch, clock):
    # arrange
    from fakes.fake_github import FakeGitHub

    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", TRACKER_TOKEN)
    gh = FakeGitHub(token=TRACKER_TOKEN).start()
    try:
        board = server.board_for("pay")
        board.update_config({"tracker": {"type": "github", "repo": "owner/name", "api_url": gh.url,
                                         "auth": {"type": "token_env", "env": "GITHUB_TOKEN"},
                                         "statuses": {"open": None, "closed": "done"}}})
        add_task(server, "A")
        board.set_stage("tracker", "user")
        post(server, "/api/p/pay/tracker/create", {"actor": "user"})
        board.set_stage("work", "user")
        sync = server.tracker_sync = TrackerSync(server, clock=clock)
        sync.tick()
        gh.issues[1]["state"] = "closed"
        clock.now += 120
        # act
        sync.tick()
        # assert
        assert task_status(server, "A") == "done"
    finally:
        gh.stop()


def test_sync__thread_runs_with_server_and_stops_with_it(tmp_home, jira):
    # arrange
    store = ProjectStore("pay")
    store.create(ProjectConfig(slug="pay", title="Платежи", chains=True))
    srv = BoardServer(port=0, tracker_sync=True)
    srv.start()
    try:
        with_jira(srv, jira)
        post(srv, "/api/p/pay/tracker/create", {"actor": "user"})
        board = srv.board_for("pay")
        board.update_config({"tracker": dict(board.store.load_config().tracker, poll_seconds=0.2, statuses=JIRA_MAP)})
        board.set_stage("work", "user")
        # act
        deadline = time.time() + 10
        while time.time() < deadline and not (board.snapshot().get("tracker_sync") or {}).get("ok"):
            time.sleep(0.05)
        thread = srv.tracker_sync._thread
        # assert
        assert board.snapshot()["tracker_sync"]["ok"] is True and thread.is_alive()
    finally:
        srv.stop()
    assert not thread.is_alive()


def test_sync__not_started_by_default(server):
    # assert
    assert server.tracker_sync._thread is None


# --- синхронизация: исправления ревью ---

@pytest.mark.parametrize("target", ["approved", "queued", "waiting_answer", "in_review", "cancelled", "blocked"])
def test_sync_config__forbidden_target__is_rejected_on_save(server, jira, target):
    # arrange
    board = server.board_for("pay")
    cfg = {"type": "jira", "url": jira.url, "project": "PROJ", "issue_type": "Task",
           "auth": {"type": "token_env", "env": "JIRA_TOKEN"}, "statuses": {"Done": target}}
    # act, assert
    with pytest.raises(ValueError, match="statuses"):
        board.update_config({"tracker": cfg})


def test_sync__in_review_in_jira_maps_to_sent(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock, statuses={"In Review": "sent"})
    sync.tick()
    jira.statuses["PROJ-1"] = "In Review"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "sent"


def test_sync__forbidden_target_in_stored_config__is_skipped(server, jira, clock):
    # arrange: старая настройка, записанная в обход проверки
    sync = sync_project(server, jira, clock)
    board = server.board_for("pay")
    config = board.store.load_config()
    config.tracker["statuses"] = {"Open": None, "Done": "approved"}
    board.store.save_config(config)
    sync.tick()
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "not_started"


def test_sync__missing_issue_in_jira__others_still_sync(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    del jira.statuses["PROJ-1"]
    jira.statuses["PROJ-2"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(server)
    assert task_status(server, "B") == "done" and state["ok"] is True and state["paused"] is False
    assert [e["key"] for e in state["errors"]] == ["A"] and "PROJ-1" in state["errors"][0]["error"]


@pytest.mark.parametrize("mode", ["header", "silent"])
def test_sync__jira_answers_as_anonymous__pauses_as_auth_error(server, jira, clock, mode):
    # arrange: просроченная cookie — Jira отвечает 200, но как анонимному пользователю
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.anonymous = mode
    clock.now += 30
    # act
    sync.tick()
    count = len(jira.requests)
    clock.now += 30
    sync.tick()
    # assert
    state = sync_state(server)
    assert state["ok"] is False and state["paused"] is True and "авторизац" in state["error"]
    assert jira.calls("GET", "/rest/api/2/myself") and len(jira.requests) == count
    assert task_status(server, "A") == "not_started"


def test_sync__all_issues_missing_but_user_known__not_paused(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.statuses.clear()
    clock.now += 30
    # act
    sync.tick()
    # assert
    state = sync_state(server)
    assert jira.calls("GET", "/rest/api/2/myself")
    assert state["ok"] is True and state["paused"] is False and sorted(e["key"] for e in state["errors"]) == ["A", "B"]


def test_sync__some_issues_found__does_not_ask_who_am_i(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    del jira.statuses["PROJ-1"]
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert not jira.calls("GET", "/rest/api/2/myself")


def test_check__jira_anonymous_header__is_auth_error(server, jira):
    # arrange
    with_jira(server, jira)
    jira.anonymous = "header"
    # act
    r = post(server, "/api/p/pay/tracker/check", {"actor": "user"}).json()
    # assert
    assert r["ok"] is False and r["kind"] == "auth"


def test_check_and_sync__plain_http_remote_tracker__warn(server, jira, clock, monkeypatch):
    # arrange: адрес поддельной Jira считается удалённым
    from agent_board import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "LOCAL_HOSTS", ())
    sync = sync_project(server, jira, clock)
    # act
    checked = post(server, "/api/p/pay/tracker/check", {"actor": "user"}).json()
    sync.tick()
    # assert
    assert checked["ok"] is True and "https" in checked["warning"]
    state = sync_state(server)
    assert state["ok"] is True and "https" in state["warning"]


def test_check_and_sync__localhost_tracker__no_warning(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    # act
    checked = post(server, "/api/p/pay/tracker/check", {"actor": "user"}).json()
    sync.tick()
    # assert
    assert checked == {"ok": True} and sync_state(server)["warning"] is None


def test_sync__jira_rejects_batch__retries_per_key(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    jira.reject_batches = True
    jira.strict_missing = True
    jira.statuses["PROJ-2"] = "Done"
    del jira.statuses["PROJ-1"]
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "B") == "done" and [e["key"] for e in sync_state(server)["errors"]] == ["A"]


def _github_project(server, gh, clock, keys=("A", "B", "C")):
    board = server.board_for("pay")
    board.update_config({"tracker": {"type": "github", "repo": "owner/name", "api_url": gh.url,
                                     "auth": {"type": "token_env", "env": "GITHUB_TOKEN"},
                                     "statuses": {"open": None, "closed": "done"}}})
    for key in keys:
        add_task(server, key)
    board.set_stage("tracker", "user")
    post(server, "/api/p/pay/tracker/create", {"actor": "user"})
    board.set_stage("work", "user")
    sync = server.tracker_sync = TrackerSync(server, clock=clock)
    return sync


@pytest.fixture
def github(monkeypatch):
    from fakes.fake_github import FakeGitHub

    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", TRACKER_TOKEN)
    fake = FakeGitHub(token=TRACKER_TOKEN).start()
    yield fake
    fake.stop()


def test_sync__github_missing_gone_moved_issues__others_still_sync(server, github, clock):
    # arrange
    sync = _github_project(server, github, clock, keys=("A", "B", "C", "D"))
    sync.tick()
    del github.issues[1]
    github.gone.add(2)
    github.moved.add(3)
    github.issues[4]["state"] = "closed"
    clock.now += 120
    # act
    sync.tick()
    # assert
    state = sync_state(server)
    assert task_status(server, "D") == "done" and state["ok"] is True and state["paused"] is False
    assert sorted(e["key"] for e in state["errors"]) == ["A", "B", "C"]


def test_sync__github_default_interval_is_120_seconds(server, github, clock):
    # arrange
    sync = _github_project(server, github, clock, keys=("A",))
    # act
    sync.tick()
    # assert
    assert sync.next_due("pay") == 120


def test_sync__first_read_done__transitions(server, jira, clock):
    # arrange: задача уже закрыта в трекере к первому чтению
    sync = sync_project(server, jira, clock)
    jira.statuses["PROJ-1"] = "Done"
    jira.statuses["PROJ-2"] = "In Progress"
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "done" and task_status(server, "B") == "not_started"


def test_sync__changed_tracker_key__counts_as_first_read(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    board = server.board_for("pay")
    board.update_task("A", "system", {"tracker_key": "PROJ-9"})
    jira.statuses["PROJ-9"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert task_status(server, "A") == "done"


def test_sync__seen_drops_cancelled_tasks(server, jira, clock):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    board = server.board_for("pay")
    board.transition("B", Status.CANCELLED, "user")
    jira.statuses["PROJ-1"] = "In Progress"
    clock.now += 30
    # act
    sync.tick()
    # assert
    seen = board.store.load_meta()["tracker_seen"]
    assert set(seen) == {"A"} and seen["A"] == ["PROJ-1", "In Progress"]


def test_sync__task_moved_meanwhile__transition_is_skipped(server, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    board = server.board_for("pay")
    real = board.transition_if
    calls = []

    def racing(key, expected, to, actor):
        calls.append(expected)
        board.transition(key, Status.IN_PROGRESS, "user")
        return real(key, expected, to, actor)

    monkeypatch.setattr(board, "transition_if", racing)
    jira.statuses["PROJ-1"] = "Done"
    clock.now += 30
    # act
    sync.tick()
    # assert
    assert calls == [Status.NOT_STARTED] and task_status(server, "A") == "in_progress"
    assert sync_state(server)["changed"] == []


def test_sync__stop_flag_between_requests__stops_promptly(server, github, clock):
    # arrange
    sync = _github_project(server, github, clock, keys=("A", "B", "C", "D"))
    before = len(github.requests)
    original = sync._stop.is_set
    sync._stop.set()
    # act
    sync.tick()
    # assert
    assert len(github.requests) == before and sync_state(server) is None
    assert original() is True


def test_sync__no_tracker__no_requests_and_no_state(server, clock):
    # arrange
    add_task(server, "A")
    server.board_for("pay").set_stage("work", "user")
    sync = server.tracker_sync = TrackerSync(server, clock=clock)
    # act
    sync.tick()
    # assert
    assert sync_state(server) is None
    assert post(server, "/api/p/pay/tracker/sync", {"actor": "user"}).status == 400


def test_sync__connection_refused__backs_off_and_does_not_pause(server, jira, clock):
    # arrange
    import socket as _socket

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    sync = sync_project(server, jira, clock, poll=30)
    board = server.board_for("pay")
    board.update_config({"tracker": dict(board.store.load_config().tracker, url="http://127.0.0.1:%d" % port)})
    delays = []
    # act
    for _ in range(3):
        sync.tick()
        delays.append(sync.next_due("pay") - clock.now)
        clock.now = sync.next_due("pay")
    # assert
    state = sync_state(server)
    assert delays == [30, 60, 120] and state["ok"] is False and state["paused"] is False


def test_sync__manual_sync_during_scheduled_tick__waits_and_applies_once(server, jira, clock):
    # arrange
    from fakes.fake_jira import FakeJira

    gate = threading.Event()

    class Gated(FakeJira):
        def route(self, method, path, body):
            if path.startswith("/rest/api/2/search") and getattr(self, "gated", False):
                gate.wait(5)
            return super().route(method, path, body)

    gated = Gated(authorization="Bearer " + TRACKER_TOKEN).start()
    try:
        gated.statuses = jira.statuses
        sync = sync_project(server, gated, clock)
        sync.tick()
        gated.statuses["PROJ-1"] = "Done"
        gated.gated = True
        clock.now += 30
        results = {}
        tick = threading.Thread(target=sync.tick)
        tick.start()
        while not any(r.path.startswith("/rest/api/2/search") for r in gated.requests[-1:]) and tick.is_alive():
            time.sleep(0.02)
        manual = threading.Thread(target=lambda: results.update(r=post(server, "/api/p/pay/tracker/sync",
                                                                      {"actor": "user"})))
        manual.start()
        time.sleep(0.2)
        waited = manual.is_alive()
        # act
        gate.set()
        tick.join(10)
        manual.join(10)
        # assert
        assert waited and results["r"].json()["ok"] is True
        changes = [e for e in events_of(server, "status_changed") if e["data"]["actor"] == "tracker"]
        assert len(changes) == 1 and task_status(server, "A") == "done"
    finally:
        gate.set()
        gated.stop()


# --- настройки проекта ---

def config_file(server):
    return json.loads((server.board_for("pay").store.root / "project.json").read_text(encoding="utf-8"))


def test_config_get__user_reads_config(server):
    # act
    r = get(server, "/api/p/pay/config?actor=user")
    # assert
    assert r.status == 200 and r.json()["config"]["slug"] == "pay" and r.json()["config"]["max_agents"] == 3


@pytest.mark.parametrize("query", ["", "?actor=agent", "?actor=project_agent"])
def test_config_get__not_user__is_forbidden(server, query):
    # act
    r = get(server, "/api/p/pay/config" + query)
    # assert
    assert r.status == 403


def test_config_set__valid_fields__saved_and_event_written(server, git_repo):
    # arrange
    fields = {"max_agents": 2, "repos": [{"name": "app", "path": str(git_repo), "base": "main"}],
              "links": [{"title": "Спека", "href": " https://example.com/spec "}],
              "ship": {"command": "git push origin {branch}", "timeout": 120},
              "tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
                          "auth": {"type": "token_env", "env": "JIRA_TOKEN"}, "statuses": {"Done": "done"}},
              "harness": {"type": "fake"}, "review": {"tool": "none"}}
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": fields})
    # assert
    assert r.status == 200 and r.json()["saved"] is True
    stored = config_file(server)
    assert stored["max_agents"] == 2 and stored["repos"] == fields["repos"] and stored["tracker"] == fields["tracker"]
    assert stored["links"] == [{"title": "Спека", "href": "https://example.com/spec"}]
    assert r.json()["config"] == get(server, "/api/p/pay/config?actor=user").json()["config"]
    changed = [e["data"] for e in get(server, "/api/p/pay/events").json()["events"] if e["type"] == "config_changed"]
    assert changed == [{"fields": sorted(fields)}]


def test_config_set__dry_run__validates_without_saving(server):
    # arrange
    before = config_file(server)
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {"max_agents": 5,
                                                                         "links": [{"title": "a", "href": " /x "}]},
                                           "dry_run": True})
    # assert
    assert r.status == 200 and r.json()["saved"] is False
    assert r.json()["config"]["max_agents"] == 5 and r.json()["config"]["links"] == [{"title": "a", "href": "/x"}]
    assert config_file(server) == before
    assert not [e for e in get(server, "/api/p/pay/events").json()["events"] if e["type"] == "config_changed"]


def test_config_set__same_values__no_event(server):
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {"max_agents": 3}})
    # assert
    assert r.status == 200
    assert not [e for e in get(server, "/api/p/pay/events").json()["events"] if e["type"] == "config_changed"]


JIRA_OK = {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
           "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}


@pytest.mark.parametrize("fields, message", [
    ({"ship": {"command": 'git push "{branch}"'}}, "кавычки"),
    ({"ship": {"command": "run $'x' {key}"}}, "$'"),
    ({"ship": {"command": "cat <<EOF\n{title}\nEOF"}}, "<<"),
    ({"ship": {"command": "run {secret}"}}, "плейсхолдер"),
    ({"ship": {"command": "true", "timeout": 0}}, "timeout"),
    ({"tracker": dict(JIRA_OK, auth={"type": "token_env", "env": "X", "token": "s3cret"})}, "auth"),
    ({"tracker": dict(JIRA_OK, password="x")}, "Недопустимые ключи"),
    ({"tracker": dict(JIRA_OK, statuses={"Review": "approved"})}, "approved"),
    ({"links": [{"title": "x", "href": "javascript:alert(1)"}]}, "ссылк"),
    ({"links": "https://x"}, "links"),
    ({"repos": [{"name": "app", "path": "/nonexistent/repo/path"}]}, "не найден"),
    ({"repos": [{"name": "app", "path": "relative/path"}]}, "абсолютн"),
    ({"harness": {"type": "gpt"}}, "харнесс"),
    ({"review": {"tool": "gerrit"}}, "ревью"),
    ({"max_agents": 0}, "max_agents"),
    ({"global_max_agents": "6"}, "global_max_agents"),
    ({"chains": "yes"}, "chains"),
    ({"branch_template": "feature/x"}, "{key}"),
    ({"branch_template": "{key}-{title}"}, "branch_template"),
    ({"resources": [{"name": "db"}, {"name": "db"}]}, "db"),
    ({"title": ""}, "title"),
    ({"unknown": 1}, "unknown"),
    ({"to_dict": 1}, "to_dict"),
    ({"stage": "work"}, "stage"),
    ({"slug": "other"}, "slug"),
    ({"project_session_id": "x"}, "project_session_id"),
])
def test_config_set__invalid__is_400_and_not_saved(server, fields, message):
    # arrange
    before = config_file(server)
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": fields})
    dry = post(server, "/api/p/pay/config", {"actor": "user", "fields": fields, "dry_run": True})
    # assert
    assert r.status == 400 and message in r.json()["error"], r.json()
    assert dry.status == 400
    assert config_file(server) == before


def test_config_set__repo_path_is_not_git__is_400(server, tmp_path):
    # arrange
    plain = tmp_path / "plain"
    plain.mkdir()
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {"repos": [{"name": "a", "path": str(plain)}]}})
    # assert
    assert r.status == 400 and "git" in r.json()["error"]


@pytest.mark.parametrize("actor", ["agent", "project_agent", "system", "tracker"])
def test_config_set__not_user__is_403(server, actor):
    # act
    r = post(server, "/api/p/pay/config", {"actor": actor, "fields": {"max_agents": 1}})
    # assert
    assert r.status == 403 and config_file(server)["max_agents"] == 3


def test_config_set__fields_not_object__is_400(server):
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": [1]})
    # assert
    assert r.status == 400


def test_config_validate__stored_config_with_missing_repo__is_400(server):
    # arrange: файл настроек поправлен вручную
    store = server.board_for("pay").store
    cfg = store.load_config()
    cfg.repos = [{"name": "app", "path": "/nonexistent/repo/path"}]
    store.save_config(cfg)
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {}, "dry_run": True})
    # assert
    assert r.status == 400 and "/nonexistent/repo/path" in r.json()["error"]


def test_config_set__keeps_conflicting__is_409(server, monkeypatch):
    # arrange
    from agent_board import board as board_module

    store = server.board_for("pay").store
    real = board_module.validate_fields

    def racing(config, names, previous):
        stored = store.load_config()
        stored.agent_rules = stored.agent_rules + "!"
        store.save_config(stored)
        real(config, names, previous)

    monkeypatch.setattr(board_module, "validate_fields", racing)
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {"agent_rules": "моя"}})
    # assert
    assert r.status == 409 and "agent_rules" in r.json()["error"]


# --- синхронизация: завершённые проекты и пул ---

class FakeWallClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def done_at(server):
    from datetime import datetime

    event = [e for e in events_of(server, "stage_changed") if e["data"]["to"] == "done"][-1]
    return datetime.fromisoformat(event["at"])


@pytest.mark.parametrize("hours, polled", [(23, True), (25, False)])
def test_sync__done_project__polled_for_24_hours_after_done(server, jira, clock, hours, polled):
    # arrange
    from datetime import timedelta

    sync = sync_project(server, jira, clock, stage="done")
    sync = server.tracker_sync = TrackerSync(server, clock=clock,
                                             wall_clock=FakeWallClock(done_at(server) + timedelta(hours=hours)))
    before = len(jira.requests)
    # act
    sync.tick()
    # assert
    assert (len(jira.requests) > before) is polled


def test_sync__done_time__remembered_in_meta(server, jira, clock):
    # arrange
    from datetime import timedelta

    sync_project(server, jira, clock, stage="done")
    sync = server.tracker_sync = TrackerSync(server, clock=clock,
                                             wall_clock=FakeWallClock(done_at(server) + timedelta(hours=1)))
    # act
    sync.tick()
    # assert
    meta = server.board_for("pay").store.load_meta()
    assert meta["stage_done_at"] == events_of(server, "stage_changed")[-1]["at"]


def _projects(*slugs):
    for slug in slugs:
        ProjectStore(slug).create(ProjectConfig(slug=slug, title=slug))


def test_sync__hung_project__does_not_delay_others(server, clock, monkeypatch):
    # arrange
    _projects("a", "b", "c")
    sync = server.tracker_sync = TrackerSync(server, clock=clock)
    sync.tick_wait = 0.3
    started, release = threading.Event(), threading.Event()
    ran = []

    def fake_tick_project(slug):
        if slug == "a":
            started.set()
            release.wait(10)
        ran.append(slug)

    monkeypatch.setattr(sync, "_tick_project", fake_tick_project)

    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    try:
        # act
        t0 = time.monotonic()
        sync.tick()
        first = time.monotonic() - t0
        sync.tick()
        # assert
        assert started.is_set() and first < 5
        assert sorted(ran) == ["b", "b", "c", "c", "pay", "pay"]
    finally:
        release.set()
        sync.stop()
    assert ran.count("a") == 1


def test_sync__projects_run_in_pool_of_at_most_4(server, clock, monkeypatch):
    # arrange
    _projects("a", "b", "c", "d", "e")
    sync = server.tracker_sync = TrackerSync(server, clock=clock)
    sync.tick_wait = 0.3
    guard, release = threading.Lock(), threading.Event()
    state = {"active": 0, "peak": 0}
    ran = []

    def fake_tick_project(slug):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        release.wait(10)
        with guard:
            state["active"] -= 1
            ran.append(slug)

    monkeypatch.setattr(sync, "_tick_project", fake_tick_project)

    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    try:
        # act
        sync.tick()
        peak_while_hung = state["peak"]
        release.set()
        deadline = time.monotonic() + 10
        while len(ran) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        sync.tick()
    finally:
        release.set()
        sync.stop()
    # assert: проекты, которым не хватило места, проходят следующим тиком
    assert peak_while_hung == 4 and state["peak"] == 4
    assert sorted(ran[:4]) == ["a", "b", "c", "d"] and set(ran) == {"a", "b", "c", "d", "e", "pay"}


def test_sync__stop__returns_promptly_with_hung_project(server, clock, monkeypatch):
    # arrange
    sync = server.tracker_sync = TrackerSync(server, clock=clock, sleep=lambda s: time.sleep(0.01))
    sync.tick_wait = 0.2
    release = threading.Event()
    monkeypatch.setattr(sync, "_tick_project", lambda slug: release.wait(30))
    monkeypatch.setattr(sync, "_is_due", lambda slug: True)
    sync.start()
    time.sleep(0.1)
    # act
    t0 = time.monotonic()
    sync.stop()
    elapsed = time.monotonic() - t0
    release.set()
    # assert
    assert elapsed < 5


def test_legacy_ship_command__project_opened_by_server__migrated(server):
    # arrange
    path = ProjectStore("old").root
    ProjectStore("old").create(ProjectConfig(slug="old", title="Старый"))
    raw = json.loads((path / "project.json").read_text(encoding="utf-8"))
    raw["ship_command"] = "git push origin {branch}"
    (path / "project.json").write_text(json.dumps(raw), encoding="utf-8")
    result = {}
    opener = threading.Thread(target=lambda: result.update(r=get(server, "/api/p/old/config?actor=user")), daemon=True)
    # act
    opener.start()
    opener.join(10)
    # assert
    assert not opener.is_alive()
    assert result["r"].status == 200 and result["r"].json()["config"]["ship"]["command"] == "git push origin {branch}"
    events = [e for e in get(server, "/api/p/old/events").json()["events"] if e["type"] == "config_migrated"]
    assert len(events) == 1


def test_sync_tick__no_project_is_due__starts_no_worker_threads(server, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    started = []
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: started.append(slug))
    # act
    sync.tick()
    # assert
    assert started == []


def test_sync_tick__sync_disabled_project__starts_no_worker_threads(server, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(server, jira, clock, stage="tracker")
    started = []
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: started.append(slug))
    # act
    sync.tick()
    # assert
    assert started == []


def test_sync_tick__project_is_due__starts_worker(server, jira, clock, monkeypatch):
    # arrange
    sync = sync_project(server, jira, clock)
    sync.tick()
    clock.now += 31
    started = []
    monkeypatch.setattr(sync, "_worker", lambda slug, lock: (started.append(slug), lock.release(), sync._slots.release()))
    # act
    sync.tick()
    # assert
    assert started == ["pay"]


def test_add_task__repo_not_in_project_repos__400(server):
    # arrange
    store = ProjectStore("pay")
    config = store.load_config()
    config.repos = [{"name": "api", "path": "/tmp/api", "base": "main"}]
    store.save_config(config)
    # act
    r = post(server, "/api/p/pay/tasks", {"task": {"key": "T-1", "title": "x", "repo": "worker"}, "actor": "user"})
    # assert
    assert r.status == 400 and "worker" in r.json()["error"]


def test_set_deps__by_agent__403(server):
    # arrange
    add_task(server)
    add_task(server, "T-2")
    # act
    r = post(server, "/api/p/pay/tasks/T-2/deps", {"deps": ["T-1"], "actor": "agent"})
    # assert
    assert r.status == 403
