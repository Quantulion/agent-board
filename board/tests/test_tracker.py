from __future__ import annotations

import base64
import json
import socket
import urllib.parse

import pytest

from agent_board import tracker
from agent_board.model import Status

from conftest import make_task
from fakes import fake_http
from fakes.fake_github import FakeGitHub
from fakes.fake_jira import FakeJira

TOKEN = "s3cr3t-T0KEN-value"
USER = "robot"
BASIC = "Basic " + base64.b64encode(("%s:%s" % (USER, TOKEN)).encode()).decode()


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIRA_TOKEN", TOKEN)
    monkeypatch.setenv("JIRA_USER", USER)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


@pytest.fixture
def jira():
    fake = FakeJira(authorization=BASIC).start()
    yield fake
    fake.stop()


@pytest.fixture
def github():
    fake = FakeGitHub(token=TOKEN).start()
    yield fake
    fake.stop()


def jira_config(url, **kw):
    cfg = {"type": "jira", "url": url, "project": "MNZ", "issue_type": "Task", "epic": None, "epic_field": None,
           "components": [], "labels": [], "extra_fields": {}, "link_type": "Blocks",
           "auth": {"type": "token_env", "env": "JIRA_TOKEN", "user_env": "JIRA_USER"}, "statuses": {}}
    cfg.update(kw)
    return cfg


def github_config(url, **kw):
    cfg = {"type": "github", "repo": "owner/name", "labels": [], "api_url": url,
           "auth": {"type": "token_env", "env": "GITHUB_TOKEN"}, "statuses": {"open": None, "closed": "done"}}
    cfg.update(kw)
    return cfg


def setup_board(board, cfg, tasks=None):
    board.update_config({"tracker": cfg})
    for task in tasks if tasks is not None else [
        make_task("A", title="Схема", description="Описание A", done_when="Миграция применена"),
        make_task("B", title="API", description="Описание B", deps=["A"]),
        make_task("C", title="UI", deps=["B"]),
        make_task("D", title="Отдельная"),
    ]:
        board.add_task(task, "user")
    board.set_stage("tracker", "user")


def all_project_text(board) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in board.store.root.rglob("*") if p.is_file())


# --- Jira ---

def test_create__jira__creates_issues_in_dependency_order_with_fields(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url, epic="MNZ-1", epic_field="customfield_10007", components=["billing"],
                                   labels=["agent"], extra_fields={"priority": {"name": "Major"}}))
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert result["errors"] == []
    tasks = board.store.load_tasks()
    assert {k: t.tracker_key for k, t in tasks.items()} == {"A": "MNZ-1", "B": "MNZ-2", "C": "MNZ-3", "D": "MNZ-4"}
    assert tasks["B"].tracker_url == jira.url + "/browse/MNZ-2"
    first = jira.calls("POST", "/rest/api/2/issue")[0]
    assert first.headers["Authorization"] == BASIC
    assert first.body == {"fields": {
        "project": {"key": "MNZ"}, "summary": "Схема", "issuetype": {"name": "Task"},
        "description": "Описание A\n\nКритерий готовности: Миграция применена",
        "components": [{"name": "billing"}], "labels": ["agent"], "customfield_10007": "MNZ-1",
        "priority": {"name": "Major"}}}
    assert jira.issues["MNZ-2"]["fields"]["description"] == "Описание B\n\nЗависит от: MNZ-1"


def test_create__jira__links_blocker_to_blocked_after_issues(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url))
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert jira.links == [
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "MNZ-1"}, "outwardIssue": {"key": "MNZ-2"}},
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "MNZ-2"}, "outwardIssue": {"key": "MNZ-3"}},
    ]
    assert result["linked"] == [["A", "B"], ["B", "C"]]
    last_issue = max(i for i, r in enumerate(jira.requests) if r.path == "/rest/api/2/issue")
    first_link = min(i for i, r in enumerate(jira.requests) if r.path == "/rest/api/2/issueLink")
    assert last_issue < first_link


def test_create__epic_without_field__uses_parent(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url, epic="MNZ-100"), [make_task("A")])
    # act
    tracker.create_issues(board, "user")
    # assert
    assert jira.issues["MNZ-1"]["fields"]["parent"] == {"key": "MNZ-100"}


def test_create__title_template__applies_key_and_title(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url, title_template="[{key}] {title}"), [make_task("A", title="Схема")])
    # act
    tracker.create_issues(board, "user")
    # assert
    assert jira.issues["MNZ-1"]["fields"]["summary"] == "[A] Схема"


def test_create__without_user_env__sends_bearer(board, monkeypatch):
    # arrange
    fake = FakeJira(authorization="Bearer " + TOKEN).start()
    try:
        setup_board(board, jira_config(fake.url, auth={"type": "token_env", "env": "JIRA_TOKEN"}), [make_task("A")])
        # act
        result = tracker.create_issues(board, "user")
    finally:
        fake.stop()
    # assert
    assert result["errors"] == [] and result["created"][0]["tracker_key"] == "MNZ-1"


def test_create__cookie_file__sends_cookie_from_file(board, tmp_path):
    # arrange
    cookie = "JSESSIONID=%s; atlassian.xsrf.token=abc" % TOKEN
    (tmp_path / "jira.txt").write_text(cookie + "\n")
    fake = FakeJira(cookie=cookie).start()
    try:
        setup_board(board, jira_config(fake.url, auth={"type": "cookie_file", "path": str(tmp_path / "jira.txt")}),
                    [make_task("A")])
        # act
        result = tracker.create_issues(board, "user")
    finally:
        fake.stop()
    # assert
    assert result["errors"] == [] and board.store.load_tasks()["A"].tracker_key == "MNZ-1"


def test_create__cancelled_tasks__are_skipped(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url), [make_task("A"), make_task("B")])
    board.transition("B", Status.CANCELLED, "user")
    # act
    tracker.create_issues(board, "user")
    # assert
    assert board.store.load_tasks()["B"].tracker_key is None and len(jira.issues) == 1


# --- частичный сбой и повтор ---

def test_create__server_error_midway__keeps_created_skips_dependents_and_rerun_finishes(board, jira):
    # arrange: второй запрос (задача B) падает
    setup_board(board, jira_config(jira.url))
    jira.fail_on[2] = (500, {"errorMessages": ["boom"]})
    # act
    first = tracker.create_issues(board, "user")
    # assert
    keys = {k: t.tracker_key for k, t in board.store.load_tasks().items()}
    assert keys == {"A": "MNZ-1", "B": None, "C": None, "D": "MNZ-2"}
    assert [e["key"] for e in first["errors"]] == ["B", "C"]
    assert "HTTP 500" in first["errors"][0]["error"] and "B" in first["errors"][1]["error"]
    assert jira.links == []
    # act: повтор
    second = tracker.create_issues(board, "user")
    # assert
    assert second["errors"] == []
    assert [c["key"] for c in second["created"]] == ["B", "C"]
    keys = {k: t.tracker_key for k, t in board.store.load_tasks().items()}
    assert keys == {"A": "MNZ-1", "B": "MNZ-3", "C": "MNZ-4", "D": "MNZ-2"}
    assert len(jira.issues) == 4
    assert [(l["inwardIssue"]["key"], l["outwardIssue"]["key"]) for l in jira.links] == [
        ("MNZ-1", "MNZ-3"), ("MNZ-3", "MNZ-4")]


def test_create__link_failure__rerun_creates_only_missing_links(board, jira):
    # arrange: 4 задачи, затем первая связь падает
    setup_board(board, jira_config(jira.url))
    jira.fail_on[5] = (503, None)
    # act
    first = tracker.create_issues(board, "user")
    second = tracker.create_issues(board, "user")
    third = tracker.create_issues(board, "user")
    # assert
    assert [e["key"] for e in first["errors"]] == ["B"] and first["linked"] == [["B", "C"]]
    assert second == {"created": [], "linked": [["A", "B"]], "errors": [], "warnings": [], "events": second["events"]}
    assert third["created"] == [] and third["linked"] == []
    assert len(jira.links) == 2 and len(jira.issues) == 4


def test_create__auth_error__stops_without_hammering(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url))
    jira.fail_all = (401, None)
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert len(jira.requests) == 1
    assert result["created"] == [] and len(result["errors"]) == 1
    assert "авторизац" in result["errors"][0]["error"]


# --- секреты ---

def test_create__401__error_is_clear_and_token_never_leaks(board, jira):
    # arrange: трекер возвращает токен в теле ошибки
    setup_board(board, jira_config(jira.url))
    jira.fail_all = (401, {"errorMessages": ["bad credentials for %s / %s" % (TOKEN, BASIC)]})
    # act
    result = tracker.create_issues(board, "user")
    # assert
    error = result["errors"][0]["error"]
    assert "HTTP 401" in error and "JIRA_TOKEN" in error
    text = json.dumps(result, ensure_ascii=False) + all_project_text(board)
    assert TOKEN not in text and BASIC.split()[1] not in text


def test_create__400_message__is_surfaced_scrubbed(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url), [make_task("A")])
    jira.fail_all = (400, {"errorMessages": ["Bad %s" % TOKEN], "errors": {"components": "Component 'x' is not valid"}})
    # act
    result = tracker.create_issues(board, "user")
    # assert
    error = result["errors"][0]["error"]
    assert "Component 'x' is not valid" in error and "Bad ***" in error and TOKEN not in error


def test_create__success__token_not_in_project_files(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url))
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert result["errors"] == []
    assert TOKEN not in all_project_text(board) + json.dumps(result, ensure_ascii=False)


def test_secret__repr_and_str_are_masked():
    # arrange
    secret = tracker.Secret(TOKEN)
    # act / assert
    assert repr(secret) == "***" and str(secret) == "***" and TOKEN not in "%s %r" % (secret, [secret])
    assert secret.reveal() == TOKEN


def test_scrub__masks_every_secret():
    # arrange, act
    text = tracker.scrub("a %s b %s" % (TOKEN, "other-secret"), [tracker.Secret(TOKEN), "other-secret"])
    # assert
    assert text == "a *** b ***"


# --- GitHub ---

def test_create__github__body_mentions_dependencies_and_labels(board, github):
    # arrange
    setup_board(board, github_config(github.url, labels=["agent"]))
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert result["errors"] == []
    tasks = board.store.load_tasks()
    assert tasks["B"].tracker_key == "#2" and tasks["B"].tracker_url.endswith("/owner/name/issues/2")
    assert github.issues[2] == {"title": "API", "body": "Описание B\n\nЗависит от: #1", "labels": ["agent"],
                                "state": "open"}
    assert github.issues[1]["body"] == "Описание A\n\nКритерий готовности: Миграция применена"
    assert github.calls("POST", "/repos/owner/name/issues")[0].headers["Authorization"] == "Bearer " + TOKEN
    assert github.comments == {} and result["linked"] == [["A", "B"], ["B", "C"]]


def test_create__github_dependency_added_after_creation__links_by_comment(board, github):
    # arrange: задачи заведены, потом пользователь добавил зависимость
    setup_board(board, github_config(github.url), [make_task("A"), make_task("B")])
    tracker.create_issues(board, "user")
    board.set_deps("B", ["A"], "user")
    # act
    result = tracker.create_issues(board, "user")
    again = tracker.create_issues(board, "user")
    # assert
    assert result["errors"] == [] and result["linked"] == [["A", "B"]]
    assert github.comments == {2: ["Зависит от: #1"]} and again["linked"] == []


def test_create__jira_dependency_added_after_creation__links(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url), [make_task("A"), make_task("B")])
    tracker.create_issues(board, "user")
    board.set_deps("B", ["A"], "user")
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert result["linked"] == [["A", "B"]] and len(jira.links) == 1 and len(jira.issues) == 2


def test_create__github_401__token_not_leaked(board, github):
    # arrange
    setup_board(board, github_config(github.url))
    github.fail_all = (401, {"message": "Bad credentials " + TOKEN})
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert "GITHUB_TOKEN" in result["errors"][0]["error"]
    assert TOKEN not in json.dumps(result, ensure_ascii=False) + all_project_text(board)


# --- проверки запроса ---

def test_create__not_tracker_stage__is_rejected(board, jira):
    # arrange
    board.update_config({"tracker": jira_config(jira.url)})
    # act / assert
    with pytest.raises(ValueError, match="tracker"):
        tracker.create_issues(board, "user")
    assert jira.requests == []


@pytest.mark.parametrize("actor", ["agent", "system", "project_agent", "tracker"])
def test_create__not_user__is_forbidden(board, jira, actor):
    # arrange
    setup_board(board, jira_config(jira.url))
    # act / assert
    with pytest.raises(PermissionError):
        tracker.create_issues(board, actor)


def test_create__no_tracker__is_rejected(board):
    # arrange
    board.set_stage("tracker", "user")
    # act / assert
    with pytest.raises(ValueError, match="трекер"):
        tracker.create_issues(board, "user")


def test_create__missing_env__names_variable(board, jira, monkeypatch):
    # arrange
    setup_board(board, jira_config(jira.url))
    monkeypatch.delenv("JIRA_TOKEN")
    # act / assert
    with pytest.raises(tracker.TrackerError, match="JIRA_TOKEN"):
        tracker.create_issues(board, "user")
    assert jira.requests == []


def test_create__emits_tracker_created_event(board, jira):
    # arrange
    setup_board(board, jira_config(jira.url), [make_task("A")])
    # act
    result = tracker.create_issues(board, "user")
    # assert
    created = [e for e in result["events"] if e["type"] == "tracker_created"]
    assert [e["data"] for e in created] == [{"created": ["A"], "linked": [], "errors": []}]


# --- конфиг ---

@pytest.mark.parametrize("cfg, message", [
    ({"type": "redmine"}, "type"),
    ({"type": "jira", "url": "http://x", "project": "MNZ", "issue_type": "Task",
      "auth": {"type": "token_env", "env": "JIRA_TOKEN", "token": "raw"}}, "token"),
    ({"type": "jira", "url": "ftp://x", "project": "MNZ", "issue_type": "Task",
      "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}, "url"),
    ({"type": "jira", "url": "http://x", "issue_type": "Task", "auth": {"type": "token_env", "env": "T"}}, "project"),
    ({"type": "github", "repo": "nope", "auth": {"type": "token_env", "env": "T"}}, "repo"),
    ({"type": "github", "repo": "o/n", "auth": {"type": "basic", "password": "x"}}, "auth"),
    ({"type": "github", "repo": "o/n", "auth": {"type": "token_env", "env": "T"}, "title_template": "{oops}"}, "oops"),
    ({"type": "jira", "url": "http://x", "project": "MNZ", "issue_type": "Task",
      "auth": {"type": "token_env", "env": "T"}, "statuses": {"Done": "finished"}}, "finished"),
])
def test_update_config__invalid_tracker__is_rejected(board, cfg, message):
    # act / assert
    with pytest.raises(ValueError, match=message):
        board.update_config({"tracker": cfg})


def test_agent_cannot_forge_tracker_key(board):
    # arrange
    board.add_task(make_task("A"), "user")
    # act / assert
    for actor in ("agent", "user"):
        with pytest.raises(PermissionError):
            board.update_task("A", actor, {"tracker_key": "MNZ-9"})


# --- проверка доступа и статусы ---

def test_check__jira_ok(board, jira):
    # arrange
    board.update_config({"tracker": jira_config(jira.url)})
    # act
    tracker.check(board.store.load_config())
    # assert
    assert [r.path for r in jira.requests] == ["/rest/api/2/myself", "/rest/api/2/project/MNZ"]


def test_check__jira_unknown_project__names_it(board, jira):
    # arrange
    board.update_config({"tracker": jira_config(jira.url, project="NOPE")})
    # act / assert
    with pytest.raises(tracker.TrackerError, match="NOPE"):
        tracker.check(board.store.load_config())


def test_check__github_401__is_auth_error_without_token(board, github):
    # arrange
    board.update_config({"tracker": github_config(github.url)})
    github.fail_all = (401, {"message": TOKEN})
    # act
    with pytest.raises(tracker.TrackerError) as info:
        tracker.check(board.store.load_config())
    # assert
    assert info.value.kind == "auth" and TOKEN not in str(info.value)


def free_port() -> int:
    """Порт только что закрытого сокета: на нём никто не слушает."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_check__unreachable__is_network_error():
    # arrange: порт без слушателя
    cfg = jira_config("http://127.0.0.1:%d" % free_port())
    # act / assert
    with pytest.raises(tracker.TrackerError) as info:
        tracker.adapter_for(cfg).check()
    assert info.value.kind == "network"


def test_get_statuses__jira_and_github(jira, github):
    # arrange
    j = tracker.adapter_for(jira_config(jira.url))
    g = tracker.adapter_for(github_config(github.url))
    a = j.create_issue(make_task("A"))
    b = g.create_issue(make_task("B"))
    jira.statuses[a["key"]] = "In Review"
    github.issues[1]["state"] = "closed"
    # act / assert
    assert j.get_statuses([a["key"]]) == {"MNZ-1": "In Review"}
    assert g.get_statuses([b["key"]]) == {"#1": "closed"}
    assert j.get_statuses([]) == {}


def test_none_adapter__check_is_rejected():
    # act / assert
    with pytest.raises(tracker.TrackerError):
        tracker.adapter_for({"type": "none"}).check()


def test_create__token_with_control_chars__error_without_token(board, jira, monkeypatch):
    # arrange: перевод строки внутри токена делает заголовок недопустимым
    monkeypatch.setenv("JIRA_TOKEN", "first-half\nsecond-half-secret")
    setup_board(board, jira_config(jira.url, auth={"type": "token_env", "env": "JIRA_TOKEN"}), [make_task("A")])
    # act
    with pytest.raises(tracker.TrackerError) as info:
        tracker.create_issues(board, "user")
    # assert
    assert "JIRA_TOKEN" in str(info.value) and "half" not in str(info.value) and jira.requests == []


def test_create__multiline_cookie_file__is_joined(board, tmp_path):
    # arrange
    (tmp_path / "jira.txt").write_text("JSESSIONID=%s;\natlassian.xsrf.token=abc\n" % TOKEN)
    fake = FakeJira(cookie="JSESSIONID=%s; atlassian.xsrf.token=abc" % TOKEN).start()
    try:
        setup_board(board, jira_config(fake.url, auth={"type": "cookie_file", "path": str(tmp_path / "jira.txt")}),
                    [make_task("A")])
        # act
        result = tracker.create_issues(board, "user")
    finally:
        fake.stop()
    # assert
    assert result["errors"] == []



# --- сетевые сбои ---

def test_create__tracker_hangs__stops_after_one_request_and_rerun_continues(board, jira, monkeypatch):
    # arrange: первый запрос висит дольше таймаута
    monkeypatch.setattr(tracker, "TIMEOUT", 0.3)
    setup_board(board, jira_config(jira.url))
    jira.fail_on[1] = fake_http.HANG
    # act
    first = tracker.create_issues(board, "user")
    # assert
    assert len(jira.requests) == 1 and first["created"] == []
    assert [e["key"] for e in first["errors"]] == ["A"] and "не ответил" in first["errors"][0]["error"]
    # act: трекер ожил
    second = tracker.create_issues(board, "user")
    # assert
    assert second["errors"] == [] and [c["key"] for c in second["created"]] == ["A", "B", "C", "D"]


def test_create__tracker_refuses__stops_after_first_task(board):
    # arrange
    setup_board(board, jira_config("http://127.0.0.1:%d" % free_port()))
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert len(result["errors"]) == 1 and "Нет связи" in result["errors"][0]["error"]


def test_create__network_error_on_link__stops_linking(board, jira, monkeypatch):
    # arrange: 4 задачи, связи — 5-й и 6-й запросы, 5-й висит
    monkeypatch.setattr(tracker, "TIMEOUT", 0.3)
    setup_board(board, jira_config(jira.url))
    jira.fail_on[5] = fake_http.HANG
    # act
    result = tracker.create_issues(board, "user")
    # assert
    assert len(jira.requests) == 5 and result["linked"] == [] and [e["key"] for e in result["errors"]] == ["B"]


def test_call__truncated_response__is_network_error(jira):
    # arrange
    jira.fail_all = fake_http.TRUNCATE
    # act / assert
    with pytest.raises(tracker.TrackerError) as info:
        tracker.adapter_for(jira_config(jira.url)).create_issue(make_task("A"))
    assert info.value.kind == "network"


def test_call__redirect__is_not_followed(jira):
    # arrange: трекер перенаправляет на другой сервер
    other = FakeJira().start()
    try:
        jira.fail_all = (302, None, {"Location": other.url + "/rest/api/2/myself"})
        # act
        with pytest.raises(tracker.TrackerError) as info:
            tracker.adapter_for(jira_config(jira.url)).check()
    finally:
        other.stop()
    # assert
    assert info.value.kind == "auth" and "HTTP 302" in str(info.value)
    assert other.requests == [] and len(jira.requests) == 1


def test_call__429__is_retryable(jira):
    # arrange
    jira.fail_all = (429, {"errorMessages": ["slow down"]})
    # act / assert
    with pytest.raises(tracker.TrackerError) as info:
        tracker.adapter_for(jira_config(jira.url)).check()
    assert info.value.kind == "retryable" and "HTTP 429" in str(info.value)


# --- ссылки и статусы ---

def test_create__non_http_issue_url__is_not_stored(board, github):
    # arrange
    github.html_url = "javascript:alert({number})"
    setup_board(board, github_config(github.url), [make_task("A")])
    # act
    result = tracker.create_issues(board, "user")
    # assert
    task = board.store.load_tasks()["A"]
    assert task.tracker_key == "#1" and task.tracker_url is None
    assert result["created"][0]["url"] is None
    assert [w["key"] for w in result["warnings"]] == ["A"] and "javascript" not in json.dumps(result)


def test_get_statuses__non_object_response__is_validation_error(jira):
    # arrange
    jira.search_response = ["not", "an", "object"]
    # act / assert
    with pytest.raises(tracker.TrackerError) as info:
        tracker.adapter_for(jira_config(jira.url)).get_statuses(["MNZ-1"])
    assert info.value.kind == "validation"


def test_get_statuses__github_non_object_response__is_validation_error(github):
    # arrange
    adapter = tracker.adapter_for(github_config(github.url))
    adapter.create_issue(make_task("A"))
    github.fail_all = (200, ["x"])
    # act / assert
    with pytest.raises(tracker.TrackerError) as info:
        adapter.get_statuses(["#1"])
    assert info.value.kind == "validation"


def test_get_statuses__custom_search_path(jira):
    # arrange
    adapter = tracker.adapter_for(jira_config(jira.url, jira_search_path="/rest/api/2/search/jql"))
    key = adapter.create_issue(make_task("A"))["key"]
    # act
    statuses = adapter.get_statuses([key])
    # assert
    assert statuses == {"MNZ-1": "Open"} and jira.requests[-1].path.startswith("/rest/api/2/search/jql?")


def test_update_config__bad_search_path__is_rejected(board):
    # act / assert
    with pytest.raises(ValueError, match="jira_search_path"):
        board.update_config({"tracker": jira_config("http://x", jira_search_path="https://evil/search")})


# --- маскирование ---

def test_scrub__masks_json_escaped_and_url_encoded_forms():
    # arrange
    secret = 'a/b+c"d\\e f&g'
    text = " | ".join([json.dumps({"t": secret}), urllib.parse.quote(secret, safe=""),
                       urllib.parse.quote_plus(secret)])
    # act
    scrubbed = tracker.scrub(text, [tracker.Secret(secret)])
    # assert
    assert scrubbed == '{"t": "***"} | *** | ***'
