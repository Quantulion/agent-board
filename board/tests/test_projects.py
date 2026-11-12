from __future__ import annotations

import pytest

from agent_board import projects
from agent_board.config import ProjectConfig
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


def test_create_project__global_limit_copied_from_existing(tmp_home):
    # arrange
    _project("a", global_max_agents=15)
    _project("b", global_max_agents=8)

    # act
    config = projects.create_project("X", "X", "free", {})

    # assert
    assert config.global_max_agents == 15


def test_create_project__no_projects__default_global_limit(tmp_home):
    # act
    config = projects.create_project("X", "X", "free", {})

    # assert
    assert config.global_max_agents == ProjectConfig(slug="x", title="x").global_max_agents


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
        "harness_type": "codex", "language": "en", "max_agents": 5, "auto_approve": True,
        "project_agent_tasks": False})

    # assert
    saved = ProjectStore("x").load_config()
    assert saved.harness["type"] == "codex"
    assert (saved.language, saved.max_agents, saved.auto_approve, saved.project_agent_tasks) == ("en", 5, True, False)
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
