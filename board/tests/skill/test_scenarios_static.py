from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from test_skill_text import SKILL, SKILL_DIR, _frontmatter

SCENARIOS = Path(__file__).resolve().parent / "scenarios"
SCENARIO_FILES = ["plan-6-tasks.md", "ready-list.md", "resume-board.md", "secret-token.md"]


def test_skill__frontmatter__only_name_and_description_with_valid_name():
    # act
    fields = _frontmatter()
    # assert
    assert set(fields) == {"name", "description"}
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    assert fields["name"] == SKILL_DIR.name
    assert fields["description"] and "<" not in fields["description"]


def test_skill__wrapper__is_executable_posix_sh():
    # arrange
    wrapper = SKILL_DIR / "scripts" / "agent-board"
    # act
    mode = wrapper.stat().st_mode
    first_line = wrapper.read_text(encoding="utf-8").splitlines()[0]
    # assert
    assert mode & stat.S_IXUSR and os.access(str(wrapper), os.X_OK)
    assert first_line in ("#!/bin/sh", "#!/usr/bin/env sh")


@pytest.mark.parametrize("name", SCENARIO_FILES)
def test_scenarios__file__has_message_answers_and_expected_state(name):
    # arrange
    text = (SCENARIOS / name).read_text(encoding="utf-8")
    # assert
    assert "## User's first message" in text
    assert "{repo}" in text
    assert "## Expected end state" in text
    assert "check_state.py" in text


def test_scenarios__setup_answers__cover_the_interview():
    # arrange
    text = (SCENARIOS / "plan-6-tasks.md").read_text(encoding="utf-8")
    # assert
    for part in ("harness `fake`", "Review: none", "Tracker: none", "Ship command: none", "Decomposition rules"):
        assert part in text


def test_scenarios__readme__describes_prompt_and_check():
    # arrange
    text = (SCENARIOS / "README.md").read_text(encoding="utf-8")
    # assert
    assert "act as the agent in a terminal session" in text and "check_state.py" in text and "make_env.py" in text
    assert all(name in text for name in SCENARIO_FILES)
