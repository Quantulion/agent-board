from __future__ import annotations

import re
from pathlib import Path
from string import Template
from typing import Any, Dict, List

from .config import ProjectConfig
from .model import Event, Task

_DIR = Path(__file__).parent / "prompts"
_NONE = "(none)"


def _render(name: str, values: Dict[str, Any]) -> str:
    # Template, а не format: значения с { } и $ подставляются как есть и не раскрываются повторно.
    text = (_DIR / name).read_text(encoding="utf-8")
    return Template(text).substitute({k: str(v) for k, v in values.items()}).strip() + "\n"


def _or_none(value: str) -> str:
    return value.strip() if value and value.strip() else _NONE


def _resources_text(config: ProjectConfig) -> str:
    names = []
    for r in config.resources:
        name = r.get("name") if isinstance(r, dict) else r
        if name:
            names.append(str(name))
    if not names:
        return "No shared resources are configured for this project."
    return "Shared resources of this project: %s." % ", ".join(names)


def task_system(config: ProjectConfig, board_cli: str = "agent-board") -> str:
    return _render("task_system.md", {
        "board_cli": board_cli,
        "language": config.language,
        "resources_text": _resources_text(config),
    })


def task_prompt(task: Task, config: ProjectConfig, worktree: Path, base: str, board_cli: str) -> str:
    deps = "- Depends on: %s" % ", ".join(task.deps) if task.deps else ""
    return _render("task.md", {
        "board_cli": board_cli,
        "key": task.key,
        "title": task.title,
        "description": _or_none(task.description),
        "done_when": _or_none(task.done_when),
        "kind": task.kind,
        "repo": task.repo or _NONE,
        "worktree": worktree,
        "base": base,
        "deps_text": deps,
        "agent_rules": _or_none(config.agent_rules),
    })


def _snapshot_text(snapshot: dict) -> str:
    lines = []
    for t in snapshot.get("tasks", []):
        line = "- %s [%s] %s" % (t["key"], t.get("display_status") or t.get("status"), t.get("title", ""))
        if t.get("open_deps"):
            line += " (waiting for: %s)" % ", ".join(t["open_deps"])
        elif t.get("deps"):
            line += " (deps: %s)" % ", ".join(t["deps"])
        lines.append(line)
    open_props = [p for p in snapshot.get("proposals", []) if p.get("status") == "open"]
    if open_props:
        lines.append("Open proposals: %s" % ", ".join("%s (%s)" % (p["id"], p["kind"]) for p in open_props))
    return "\n".join(lines) if lines else "No tasks yet."


def project_prompt(config: ProjectConfig, snapshot: dict, board_cli: str) -> str:
    return _render("project.md", {
        "board_cli": board_cli,
        "title": config.title,
        "slug": config.slug,
        "language": config.language,
        "project_brief": _or_none(config.project_brief),
        "snapshot_text": _snapshot_text(snapshot),
        "agent_rules": _or_none(config.agent_rules),
    })


def _quote(text: str) -> str:
    # Дословная цитата в блоке с забором длиннее любой серии backtick внутри текста.
    runs = re.findall(r"`+", text)
    fence = "`" * max(3, max((len(r) for r in runs), default=0) + 1)
    return "%s\n%s\n%s" % (fence, text, fence)


def _wake_line(r: dict) -> str:
    reason = r.get("reason", "")
    if reason == "message":
        return "New message from the user (id %s), verbatim:\n%s" % (r.get("message_id", "?"), _quote(str(r.get("text", ""))))
    if reason == "answer":
        out = "The user answered your question%s." % (
            " (%s)" % r["question_id"] if r.get("question_id") else "")
        if r.get("question"):
            out += "\nQuestion: %s" % r["question"]
        out += "\nChosen: %s" % (r.get("choice") or "(no option chosen)")
        if r.get("comment"):
            out += "\nComment, verbatim:\n%s" % _quote(str(r["comment"]))
        return out
    if reason == "returned":
        return ("The task was returned to work from status `%s`. Find out why in the card and thread, "
                "then continue." % r.get("from", "?"))
    if reason == "rebase":
        base = r.get("base", "the base branch")
        head = "The branch of dependency %s changed; rebase onto %s." % (r.get("from", "?"), base)
        if r.get("rebase_ok") is True:
            return head + " The rebase was applied cleanly; check that the work still fits and continue."
        if r.get("rebase_ok") is False:
            return head + " The automatic rebase hit conflicts; resolve them in your worktree, or ask a question on the card if you cannot."
        return head + " Do it and resolve conflicts yourself; ask a question on the card if you cannot."
    if reason == "interrupted":
        return ("Your previous turn was interrupted by a server restart. Check the actual state of the "
                "worktree and the card, then continue.")
    if reason == "escalation":
        return "Escalation from task agent %s in the project thread. Read it with thread _project and respond." % r.get("from", "?")
    if reason == "proposal_rejected":
        return "The user rejected your proposal%s. Do not repeat it; adapt." % (
            " (%s)" % r["id"] if r.get("id") else "")
    if reason == "unblocked":
        return "The task is unblocked: its dependencies are done or the external blocker was removed. Start working."
    if reason in ("ready", "manual"):
        return "Start working on the task."
    return "Woken: %s." % (reason or "unknown reason")


def wake_message(reasons: List[dict], digest: str) -> str:
    parts = [_wake_line(r) for r in reasons]
    out = "\n\n".join(parts)
    if digest and digest.strip():
        out += "\n\n## Board changes since your last turn\n" + digest.strip()
    return out + "\n"


def _event_line(e: Event):
    d = e.data
    k = d.get("key", "?")
    t = e.type
    if t == "status_changed":
        return "%s: %s -> %s (by %s)" % (k, d.get("from"), d.get("to"), d.get("actor", "?"))
    if t == "question_asked":
        return "%s: question %s asked" % (k, d.get("question_id", "?"))
    if t == "question_answered":
        return "%s: question %s answered" % (k, d.get("question_id", "?"))
    if t == "proposal_applied":
        return "proposal %s applied" % d.get("id", "?")
    if t == "proposal_rejected":
        return "proposal %s rejected" % d.get("id", "?")
    if t == "turn_failed":
        return "%s: agent turn FAILED: %s" % (k, str(d.get("error", "")).strip()[:200])
    if t == "task_added":
        return "%s: task added" % k
    if t == "deps_changed":
        return "%s: deps now [%s]" % (k, ", ".join(d.get("deps", [])))
    if t == "agent_wake" and d.get("reason") == "escalation":
        return "%s escalated to the project thread" % d.get("from", k)
    return None


def digest(events: List[Event], limit: int = 40) -> str:
    lines = []
    for e in events:
        line = _event_line(e)
        if line:
            lines.append("#%d %s" % (e.seq, line))
    if len(lines) > limit:
        hidden = len(lines) - limit
        lines = ["…ещё %d событий" % hidden] + lines[-limit:]
    return "\n".join(lines)
