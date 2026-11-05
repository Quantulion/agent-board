from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

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
    assert "Доска запускается" in r.body.decode()
    # куки достаточно для API
    r2 = get(server, "/api/projects", token=None, headers={"Cookie": "board_token=%s" % server.token})
    assert r2.status == 200


def test_static_path_traversal_blocked(tmp_home, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("hi")
    (tmp_path / "secret.txt").write_text("secret")
    srv = BoardServer(port=0, static_dir=static)
    srv.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/../secret.txt")
        assert c.getresponse().status in (400, 403, 404)
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/%2e%2e/secret.txt")
        r = c.getresponse()
        assert r.status in (400, 403, 404) and b"secret" not in r.read()
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        c.request("GET", "/index.html")
        assert c.getresponse().read() == b"hi"
    finally:
        srv.stop()


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
    assert wakes == [{"key": "_project", "reason": "escalation", "from": "T-1"}]


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
