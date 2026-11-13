from __future__ import annotations

import json

import pytest

from agent_board import projects
from agent_board.config import ProjectConfig
from agent_board.settings import load_settings, update_settings
from agent_board.store import ProjectStore

from conftest import make_task


def _project(slug, **kw):
    store = ProjectStore(slug)
    store.create(ProjectConfig(slug=slug, title=slug, **kw))
    return store


def test_unique_project_key__free__kept():
    # arrange, act, assert
    assert projects.unique_project_key("FIX", ["pay"]) == "FIX"


def test_unique_project_key__taken_by_slug__numbered():
    # arrange, act, assert
    assert projects.unique_project_key("fix", ["FIX", "fix2"]) == "fix3"


def test_task_prefixes__keys_of_every_project__upper_prefixes(tmp_home):
    # arrange
    store = _project("agent-board")
    store.save_task(make_task("AB-1"))
    store.save_task(make_task("ab-12"))
    store.save_task(make_task("misc"))
    _project("pay")

    # act
    prefixes = projects.task_prefixes()

    # assert
    assert prefixes == {"agent-board": {"AB"}, "pay": set()}


def test_create_project__key_is_task_prefix_of_other_project__numbered(tmp_home):
    # arrange
    _project("agent-board").save_task(make_task("AB-1"))

    # act
    config = projects.create_project("X", "AB", "free", {})

    # assert
    assert config.slug == "ab2"


def test_create_project__key_taken_by_slug__numbered(tmp_home):
    # arrange
    _project("fix")

    # act
    config = projects.create_project("Fixes", "Fix", "free", {})

    # assert
    assert config.slug == "fix2"
    assert ProjectStore("fix2").load_config().title == "Fixes"


def test_create_project__free__stage_work(tmp_home):
    # act
    config = projects.create_project("Fixes", "FIX", "free", {})

    # assert
    saved = ProjectStore("fix").load_config()
    assert (saved.stage, saved.kind, saved.repos) == ("work", "free", [])
    assert (config.stage, config.kind) == ("work", "free")


def test_create_project__settings_exist__no_global_limit_in_project(tmp_home):
    # arrange
    update_settings("user", {"global_max_agents": 15})

    # act
    projects.create_project("X", "X", "free", {})

    # assert
    raw = json.loads((tmp_home / "projects" / "x" / "project.json").read_text(encoding="utf-8"))
    assert "global_max_agents" not in raw and "language" not in raw
    assert load_settings().global_max_agents == 15


@pytest.mark.parametrize("harness_type", ["claude", "codex"])
def test_create_project__harness_type_as_board__no_override(tmp_home, harness_type):
    # arrange
    update_settings("user", {"harness": {"type": harness_type}})

    # act
    projects.create_project("X", "X", "free", {"harness_type": harness_type})

    # assert
    raw = json.loads((tmp_home / "projects" / "x" / "project.json").read_text(encoding="utf-8"))
    assert raw["harness"] is None
    saved = ProjectStore("x").load_config()
    assert saved.harness == {"type": harness_type} and saved.overrides == set()


def test_create_project__harness_type_differs_from_board__override(tmp_home):
    # act
    projects.create_project("X", "X", "free", {"harness_type": "codex"})

    # assert
    saved = ProjectStore("x").load_config()
    assert saved.harness == {"type": "codex"} and saved.overrides == {"harness"}


def test_create_project__language_option__rejected(tmp_home):
    # act, assert
    with pytest.raises(ValueError):
        projects.create_project("X", "X", "free", {"language": "en"})


@pytest.mark.parametrize("key", ["F-1", "", "1AB", "ABCDEFGHIJK", None])
def test_create_project__bad_key__rejected(tmp_home, key):
    # act, assert
    with pytest.raises(ValueError):
        projects.create_project("X", key, "free", {})


@pytest.mark.parametrize("title", ["", "   ", None])
def test_create_project__empty_title__rejected(tmp_home, title):
    # act, assert
    with pytest.raises(ValueError):
        projects.create_project(title, "X", "free", {})


def test_create_project__options__applied(tmp_home):
    # act
    config = projects.create_project("X", "X", "free", {
        "harness_type": "codex", "max_agents": 5, "auto_approve": True, "project_agent_tasks": False})

    # assert
    saved = ProjectStore("x").load_config()
    assert saved.harness["type"] == "codex"
    assert (saved.max_agents, saved.auto_approve, saved.project_agent_tasks) == (5, True, False)
    assert config.to_dict() == saved.to_dict()


@pytest.mark.parametrize("options", [{"harness_type": "vim"}, {"max_agents": 0}, {"auto_approve": "yes"},
                                     {"language": ""}, {"project_agent_tasks": 1}, {"stage": "done"}])
def test_create_project__bad_option__rejected_nothing_created(tmp_home, options):
    # act
    with pytest.raises(ValueError):
        projects.create_project("X", "X", "free", options)

    # assert
    assert not (tmp_home / "projects" / "x").exists()


def test_create_project__unknown_kind__rejected(tmp_home):
    # act, assert
    with pytest.raises(ValueError):
        projects.create_project("X", "X", "scrum", {})
