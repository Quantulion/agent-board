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

from agent_board.board import Board
from agent_board.config import ProjectConfig
from agent_board import server as server_mod
from agent_board.server import DEFAULT_PORTS, BoardServer, ServerAlreadyRunning, read_discovery
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
    assert data == {"port": srv.port, "token": srv.token, "pid": os.getpid()}
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
    board.update_config({"tracker": {"type": "jira", "url": jira.url, "project": "MNZ", "issue_type": "Task",
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
    assert body["errors"] == [] and [c["tracker_key"] for c in body["created"]] == ["MNZ-1", "MNZ-2"]
    assert body["linked"] == [["A", "B"]]
    tasks = {t["key"]: t for t in get(server, "/api/p/pay/state").json()["tasks"]}
    assert tasks["B"]["tracker_url"] == jira.url + "/browse/MNZ-2"


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


def test_agent_cannot_write_tracker_key(server):
    # arrange
    add_task(server)
    # act
    r = post(server, "/api/p/pay/tasks/T-1/update", {"actor": "agent", "fields": {"tracker_key": "MNZ-9"}})
    # assert
    assert r.status == 403
