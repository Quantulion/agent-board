from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from agent_board.board import EVENT_TYPES, WAKE_REASONS
from agent_board.cli import build_parser
from agent_board.config import NOT_SETTABLE, ProjectConfig

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#+\s+(.*)$", re.M)


def _pages():
    return [ROOT / "README.md"] + sorted(DOCS.glob("*.md"))


def _anchor(heading: str) -> str:
    """Якорь заголовка по правилам GitHub."""
    text = re.sub(r"[`*]", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _subcommands():
    parser = build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    return sorted(action.choices)


def test_docs__relative_links__point_to_existing_files_and_headings():
    # arrange
    broken = []

    # act
    for page in _pages():
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            if re.match(r"^[a-z]+:", target):
                continue
            path, _, anchor = target.partition("#")
            file = (page.parent / path).resolve() if path else page
            if not file.exists():
                broken.append("%s: %s" % (page.name, target))
            elif anchor and file.suffix == ".md":
                anchors = {_anchor(h) for h in HEADING.findall(file.read_text(encoding="utf-8"))}
                if anchor not in anchors:
                    broken.append("%s: %s" % (page.name, target))

    # assert
    assert broken == []


def test_docs__cli__every_subcommand_described():
    # arrange
    text = (DOCS / "cli.md").read_text(encoding="utf-8")

    # act
    missing = [name for name in _subcommands() if "`" + name not in text]

    # assert
    assert missing == []


def test_docs__configuration__every_settable_field_described():
    # arrange
    text = (DOCS / "configuration.md").read_text(encoding="utf-8")
    names = [f.name for f in fields(ProjectConfig) if f.name not in NOT_SETTABLE]

    # act
    missing = [name for name in names if "`" + name + "`" not in text]

    # assert
    assert missing == []


def test_docs__api__every_event_and_wake_reason_described():
    # arrange
    text = (DOCS / "api.md").read_text(encoding="utf-8")

    # act
    missing = [name for name in list(EVENT_TYPES) + list(WAKE_REASONS) if "`" + name + "`" not in text]

    # assert
    assert missing == []
