from __future__ import annotations

import json

import pytest

from agent_board.board import Board
from agent_board.cli import main
from agent_board.config import ProjectConfig
from agent_board.server import BoardServer
from agent_board.settings import (
    BoardSettings, default_harness, default_review, load_settings, migrate, save_settings, settings_path,
    update_settings,
)
from agent_board.store import ProjectStore

from conftest import NOW
from test_server import call, get, open_sse, post, read_until

CODEX = {"type": "codex", "sandbox": "workspace-write"}


def _raw(home, slug):
    return json.loads((home / "projects" / slug / "project.json").read_text(encoding="utf-8"))


def _legacy_project(home, slug, **fields):
    """Проект в формате прежней версии: язык и общий лимит хранились в каждом проекте."""
    root = home / "projects" / slug
    for name in ("tasks", "threads", "reports"):
        (root / name).mkdir(parents=True)
    data = {"slug": slug, "title": slug, "language": "ru", "global_max_agents": 6,
            "harness": default_harness(), "review": default_review()}
    data.update(fields)
    (root / "project.json").write_text(json.dumps(data), encoding="utf-8")


def _project(slug="pay", **kw):
    store = ProjectStore(slug)
    store.create(ProjectConfig(slug=slug, title=slug, **kw))
    return store


def _board(store):
    return Board(store, clock=lambda: NOW)


# --- чтение и запись ---

def test_load_settings__empty_board__defaults_saved(tmp_home):
    # act
    settings = load_settings()

    # assert
    assert settings == BoardSettings()
    assert json.loads(settings_path().read_text(encoding="utf-8")) == BoardSettings().to_dict()


def test_update_settings__user__saved_and_read_back(tmp_home):
    # act
    result = update_settings("user", {"language": "en", "global_max_agents": 2, "harness": CODEX,
                                      "review": {"tool": "none"}})

    # assert
    assert result["saved"] is True
    assert result["changed"] == ["global_max_agents", "harness", "language", "review"]
    assert load_settings() == BoardSettings(language="en", global_max_agents=2, harness=CODEX,
                                            review={"tool": "none"})


def test_update_settings__dry_run__not_saved(tmp_home):
    # act
    result = update_settings("user", {"language": "en"}, dry_run=True)

    # assert
    assert result["saved"] is False and result["settings"]["language"] == "en"
    assert load_settings().language == "ru"


def test_update_settings__not_user__refused(tmp_home):
    # act, assert
    with pytest.raises(PermissionError):
        update_settings("agent", {"language": "en"})
    assert load_settings().language == "ru"


@pytest.mark.parametrize("fields", [
    {"language": ""}, {"global_max_agents": 0}, {"global_max_agents": True}, {"harness": {"type": "vim"}},
    {"harness": None}, {"review": {"tool": "gerrit"}}, {"max_agents": 2}, [],
])
def test_update_settings__bad_value__rejected_nothing_saved(tmp_home, fields):
    # act, assert
    with pytest.raises(ValueError):
        update_settings("user", fields)
    assert load_settings() == BoardSettings()


def test_load_settings__broken_field_in_file__default_for_that_field(tmp_home):
    # arrange
    settings_path().write_text(json.dumps({"language": "en", "global_max_agents": "many"}), encoding="utf-8")

    # act
    settings = load_settings()

    # assert
    assert (settings.language, settings.global_max_agents) == ("en", BoardSettings().global_max_agents)


# --- наследование и переопределение ---

def test_load_config__project_without_own_values__board_values(tmp_home):
    # arrange
    update_settings("user", {"harness": CODEX, "review": {"tool": "none"}})
    store = _project()

    # act
    config = store.load_config()

    # assert
    assert (config.harness, config.review) == (CODEX, {"tool": "none"})
    assert config.overrides == set()


def test_load_config__board_changes__inherited_value_follows_own_value_kept(tmp_home):
    # arrange
    inheriting = _project("pay")
    own = _project("fix", review={"tool": "none"})

    # act
    update_settings("user", {"harness": CODEX, "review": {"tool": "crit", "binary": "/opt/crit"}})

    # assert
    assert inheriting.load_config().harness == CODEX
    assert inheriting.load_config().review == {"tool": "crit", "binary": "/opt/crit"}
    assert own.load_config().harness == CODEX
    assert own.load_config().review == {"tool": "none"}
    assert own.load_config().overrides == {"review"}


def test_set_config__harness__becomes_override(tmp_home):
    # arrange
    store = _project()
    board = _board(store)

    # act
    result = board.set_config("user", {"harness": CODEX})

    # assert
    assert _raw(tmp_home, "pay")["harness"] == CODEX
    assert result["config"]["overrides"] == ["harness"]
    assert result["events"][0]["data"]["fields"] == ["harness"]
    update_settings("user", {"harness": {"type": "claude", "model": "x"}})
    assert store.load_config().harness == CODEX


def test_set_config__null__override_removed_board_value_applies(tmp_home):
    # arrange
    store = _project(harness=CODEX)
    board = _board(store)

    # act
    result = board.set_config("user", {"harness": None})

    # assert
    assert _raw(tmp_home, "pay")["harness"] is None
    assert result["config"]["harness"] == default_harness() and result["config"]["overrides"] == []
    assert result["events"][0]["data"]["fields"] == ["harness"]
    update_settings("user", {"harness": CODEX})
    assert store.load_config().harness == CODEX


def test_set_config__null_on_override_equal_to_board__override_removed_with_event(tmp_home):
    # arrange
    store = _project(review=default_review())
    board = _board(store)

    # act
    result = board.set_config("user", {"review": None})

    # assert
    assert _raw(tmp_home, "pay")["review"] is None
    assert result["events"][0]["data"]["fields"] == ["review"]


def test_other_config_changes__inherited_values_stay_inherited(tmp_home):
    # arrange
    store = _project()
    board = _board(store)

    # act
    board.set_config("user", {"max_agents": 5})
    board.update_config({"title": "Новое имя"})

    # assert
    raw = _raw(tmp_home, "pay")
    assert (raw["harness"], raw["review"], raw["max_agents"]) == (None, None, 5)


@pytest.mark.parametrize("name, value", [("language", "en"), ("global_max_agents", 3)])
def test_set_config__board_field__refused(tmp_home, name, value):
    # arrange
    board = _board(_project())

    # act, assert
    with pytest.raises(ValueError, match="settings set"):
        board.set_config("user", {name: value})
    assert name not in _raw(tmp_home, "pay")


def test_propose_set_config__board_field__refused(tmp_home):
    # arrange
    board = _board(_project())

    # act, assert
    with pytest.raises(ValueError, match="settings set"):
        board.propose("set_config", {"fields": {"language": "en"}}, "")


def test_propose_set_config__reset_without_override__no_change_refused(tmp_home):
    # arrange
    board = _board(_project())

    # act, assert
    with pytest.raises(ValueError):
        board.propose("set_config", {"fields": {"harness": None}}, "")


def test_snapshot__config__effective_values_board_fields_and_overrides(tmp_home):
    # arrange
    update_settings("user", {"language": "en", "global_max_agents": 9})
    board = _board(_project(review={"tool": "none"}))

    # act
    config = board.snapshot()["config"]

    # assert
    assert (config["language"], config["global_max_agents"]) == ("en", 9)
    assert config["harness"] == default_harness() and config["review"] == {"tool": "none"}
    assert config["overrides"] == ["review"]


def test_settings_changed__project_events_only_for_fields_in_effect(tmp_home):
    # arrange
    inheriting = _board(_project("pay"))
    own = _board(_project("fix", harness=CODEX, review={"tool": "none"}))

    # act
    first = inheriting.settings_changed(["harness", "language"])
    second = own.settings_changed(["harness", "review"])

    # assert
    assert [e.data for e in first] == [{"fields": ["harness", "language"], "settings": True}]
    assert second == []


# --- перенос существующих досок ---

def test_migrate__legacy_projects__limit_language_and_common_values(tmp_home):
    # arrange
    _legacy_project(tmp_home, "a", global_max_agents=4, language="en", harness=CODEX)
    _legacy_project(tmp_home, "b", global_max_agents=9, language="en")
    _legacy_project(tmp_home, "c", global_max_agents=2, language="ru", harness=CODEX, review={"tool": "none"})

    # act
    settings = load_settings()

    # assert
    assert settings == BoardSettings(language="en", global_max_agents=9, harness=CODEX, review=default_review())
    raws = {slug: _raw(tmp_home, slug) for slug in "abc"}
    assert all("language" not in r and "global_max_agents" not in r for r in raws.values())
    assert (raws["a"]["harness"], raws["b"]["harness"], raws["c"]["harness"]) == (None, default_harness(), None)
    assert (raws["a"]["review"], raws["b"]["review"], raws["c"]["review"]) == (None, None, {"tool": "none"})
    assert ProjectStore("b").load_config().overrides == {"harness"}
    assert ProjectStore("c").load_config().overrides == {"review"}


def test_migrate__legacy_project_without_harness_key__old_default_counted(tmp_home):
    # arrange
    _legacy_project(tmp_home, "a", harness=CODEX)
    _legacy_project(tmp_home, "b")
    _legacy_project(tmp_home, "c")
    data = _raw(tmp_home, "c")
    del data["harness"], data["review"]
    (tmp_home / "projects" / "c" / "project.json").write_text(json.dumps(data), encoding="utf-8")

    # act
    settings = load_settings()

    # assert
    assert settings.harness == default_harness()
    assert _raw(tmp_home, "c")["harness"] is None and _raw(tmp_home, "a")["harness"] == CODEX


def test_migrate__second_run__changes_nothing(tmp_home):
    # arrange
    _legacy_project(tmp_home, "a", global_max_agents=4, harness=CODEX)
    _legacy_project(tmp_home, "b", global_max_agents=7)
    load_settings()
    before = {p: p.read_bytes() for p in tmp_home.rglob("*.json")}

    # act
    created = migrate()
    load_settings()
    ProjectStore("a").load_config()

    # assert
    assert created is False
    assert {p: p.read_bytes() for p in tmp_home.rglob("*.json")} == before


def test_migrate__board_settings_changed_later__not_overwritten(tmp_home):
    # arrange
    _legacy_project(tmp_home, "a", language="en")
    load_settings()
    update_settings("user", {"language": "ru"})

    # act
    migrate()

    # assert
    assert load_settings().language == "ru"


def test_migrate__new_format_projects__not_counted_overrides_kept(tmp_home):
    # arrange
    _project("pay", harness=CODEX)

    # act
    settings = load_settings()

    # assert
    assert settings == BoardSettings()
    assert _raw(tmp_home, "pay")["harness"] == CODEX


def test_board__legacy_project__migrated_before_own_config_rewrite(tmp_home):
    # arrange: в прежнем формате была и старая команда отправки, которую доска переносит сама
    _legacy_project(tmp_home, "a", language="en", global_max_agents=11, ship_command="echo {key}")

    # act
    board = Board(ProjectStore("a"), clock=lambda: NOW)

    # assert
    assert (load_settings().language, load_settings().global_max_agents) == ("en", 11)
    assert board.store.load_config().ship["command"] == "echo {key}"


# --- API ---

@pytest.fixture
def server(tmp_home):
    _project("pay")
    _project("fix", harness=CODEX)
    srv = BoardServer(port=0)
    srv.start()
    yield srv
    srv.stop()


def put(server, path, body, **kw):
    return call(server, "PUT", path, body, **kw)


def test_api_get_settings__current_values(server):
    # act
    r = get(server, "/api/settings")

    # assert
    assert r.status == 200 and r.json() == {"settings": BoardSettings().to_dict()}


def test_api_get_settings__no_token__unauthorized(server):
    # act
    r = get(server, "/api/settings", token=None)

    # assert
    assert r.status == 401


def test_api_put_settings__user__saved_and_announced(server):
    # arrange
    sock = open_sse(server, path="/api/stream")
    try:
        read_until(sock, "\r\n\r\n")

        # act
        r = put(server, "/api/settings", {"actor": "user", "fields": {"harness": {"type": "claude", "model": "m"},
                                                                      "global_max_agents": 2}})

        # assert
        assert r.status == 200 and r.json()["changed"] == ["global_max_agents", "harness"]
        out = read_until(sock, '"type": "settings_changed"')
        assert '"slug": "pay"' in out and '"type": "config_changed"' in out
    finally:
        sock.close()
    assert load_settings().global_max_agents == 2
    pay = get(server, "/api/p/pay/state").json()["config"]
    assert pay["harness"] == {"type": "claude", "model": "m"} and pay["global_max_agents"] == 2
    events = get(server, "/api/p/fix/events").json()["events"]
    assert [e["data"]["fields"] for e in events if e["type"] == "config_changed"] == [["global_max_agents"]]


def test_api_put_settings__dry_run__not_saved_no_events(server):
    # act
    r = put(server, "/api/settings", {"actor": "user", "fields": {"language": "en"}, "dry_run": True})

    # assert
    assert r.status == 200 and r.json()["saved"] is False
    assert load_settings().language == "ru"
    assert get(server, "/api/p/pay/events").json()["events"] == []


@pytest.mark.parametrize("actor", ["agent", "project_agent"])
def test_api_put_settings__not_user__forbidden(server, actor):
    # act
    r = put(server, "/api/settings", {"actor": actor, "fields": {"language": "en"}})

    # assert
    assert r.status == 403
    assert load_settings().language == "ru"


def test_api_put_settings__bad_value__400(server):
    # act
    r = put(server, "/api/settings", {"actor": "user", "fields": {"global_max_agents": 0}})

    # assert
    assert r.status == 400 and "global_max_agents" in r.json()["error"]


def test_api_put_settings__foreign_origin__forbidden(server):
    # act
    r = put(server, "/api/settings", {"actor": "user", "fields": {"language": "en"}},
            headers={"Origin": "http://evil.example"})

    # assert
    assert r.status == 403


def test_api_settings_schema__fields_described(server):
    # act
    schema = get(server, "/api/settings/schema").json()

    # assert
    assert [f["name"] for f in schema["fields"]] == ["language", "global_max_agents", "harness", "review"]
    assert [f["inherited"] for f in schema["fields"]] == [False, False, True, True]


def test_api_config_set__board_field__400(server):
    # act
    r = post(server, "/api/p/pay/config", {"actor": "user", "fields": {"global_max_agents": 3}})

    # assert
    assert r.status == 400 and "settings set" in r.json()["error"]


def test_api_create_project__harness_as_board__no_override(server):
    # act
    post(server, "/api/projects", {"actor": "user", "title": "Новый", "key": "NEW", "kind": "free",
                                   "harness_type": "claude"})

    # assert
    assert get(server, "/api/p/new/config?actor=user").json()["config"]["overrides"] == []


# --- CLI ---

def _cli(capsys, *argv):
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_settings__set_then_get(server, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)

    # act
    code, out, err = _cli(capsys, "settings", "set", "--json", '{"language": "en"}')
    shown = _cli(capsys, "settings", "get", "--json")
    plain = _cli(capsys, "settings", "show")

    # assert
    assert code == 0, err
    assert json.loads(out)["changed"] == ["language"]
    assert json.loads(shown[1])["language"] == "en"
    assert "language: en" in plain[1]


def test_cli_settings__dry_run__not_saved(server, capsys, monkeypatch):
    # arrange
    monkeypatch.delenv("AGENT_BOARD_ACTOR", raising=False)

    # act
    code, out, _ = _cli(capsys, "settings", "set", "--json", '{"global_max_agents": 3}', "--dry-run")

    # assert
    assert code == 0 and json.loads(out)["saved"] is False
    assert load_settings().global_max_agents == BoardSettings().global_max_agents


def test_cli_settings__agent__refused(server, capsys, monkeypatch):
    # arrange
    monkeypatch.setenv("AGENT_BOARD_ACTOR", "agent")

    # act
    code, _, err = _cli(capsys, "settings", "set", "--json", '{"language": "en"}')

    # assert
    assert code == 1 and err.strip()
    assert load_settings().language == "ru"


def test_cli_settings_schema__without_board(tmp_home, capsys):
    # act
    code, out, _ = _cli(capsys, "settings", "schema", "--json")
    table = _cli(capsys, "settings", "schema")

    # assert
    assert code == 0 and {f["name"] for f in json.loads(out)["fields"]} == {
        "language", "global_max_agents", "harness", "review"}
    assert "global_max_agents" in table[1]


def test_cli_config_schema__no_board_fields(tmp_home, capsys):
    # act
    code, out, _ = _cli(capsys, "config", "schema", "--json")

    # assert
    names = {f["name"] for f in json.loads(out)["fields"]}
    assert code == 0 and not names & {"language", "global_max_agents"} and {"harness", "review"} <= names
