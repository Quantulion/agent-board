from __future__ import annotations

import re
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

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


def _resources_section(config: ProjectConfig, board_cli: str) -> str:
    items = []
    for r in config.resources:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        desc = r.get("description")
        items.append("- %s%s" % (r["name"], " — %s" % desc if desc else ""))
    if not items:
        return ""
    return (
        "## Shared resources\n"
        "These resources are shared and must be used exclusively:\n%s\n"
        "Run any command that touches them only as `%s lock-run <resource> -- <command>`, never directly. "
        "The call blocks while waiting in a queue (visible on the card), so run it with a long tool timeout "
        "or in the background.\n" % ("\n".join(items), board_cli)
    )


def task_system(config: ProjectConfig, board_cli: str) -> str:
    return _render("task_system.md", {
        "board_cli": board_cli,
        "language": config.language,
        "resources_section": _resources_section(config, board_cli),
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


def _read_hint(cmd: str, what: str) -> str:
    return "%s is missing here; read it from the board with `%s`." % (what, cmd)


def _wake_line(r: dict, cli: str) -> str:
    reason = r.get("reason", "")
    key = r.get("key") or "<KEY>"
    thread = "%s thread %s" % (cli, key)
    if reason == "message":
        if r.get("text"):
            return "New message from the user (id %s), verbatim:\n%s" % (r.get("message_id", "?"), _quote(str(r["text"])))
        return "New message from the user. " + _read_hint(thread, "Its text")
    if reason == "answer":
        out = "The user answered your question%s." % (" (%s)" % r["question_id"] if r.get("question_id") else "")
        if r.get("question"):
            out += "\nQuestion: %s" % r["question"]
        if r.get("choice"):
            out += "\nChosen: %s" % r["choice"]
        if r.get("comment"):
            out += "\nComment, verbatim:\n%s" % _quote(str(r["comment"]))
        if not r.get("choice") and not r.get("comment"):
            out += " " + _read_hint("%s show %s" % (cli, key), "The answer")
        return out
    if reason == "returned":
        origin = "from status `%s`" % r["from"] if r.get("from") else \
            "(the previous status is missing here; see `%s show %s`)" % (cli, key)
        return ("The task was returned to work %s. Find out why in the card and thread, then continue." % origin)
    if reason == "rebase":
        base = r.get("base") or "the new base"
        dep = "dependency %s" % r["from"] if r.get("from") else \
            "a dependency (which one: see `%s show %s`)" % (cli, key)
        files = r.get("conflict_files") or []
        if r.get("merge_branches"):
            return ("The branches of your dependencies changed: %s. The board does not merge several dependency "
                    "branches into your worktree by itself. Do not run any git commands; ask the user on the card "
                    "to merge them." % ", ".join(r["merge_branches"]))
        if r.get("carried_ref"):
            moved = "moved your branch onto %s" % base if r.get("rebase_ok") is not False else \
                "could not move your branch onto %s" % base
            return ("The branch of %s changed; the board %s, but could not put your uncommitted work back into "
                    "the files. Your work is not lost: it is saved in git at `%s`. Do not run any git commands and "
                    "do not redo the work; ask the user on the card with `%s ask %s` to restore it from that ref, "
                    "then continue." % (dep, moved, r["carried_ref"], cli, key))
        if r.get("rebase_ok") is False:
            return ("The branch of %s changed; the board could not move your work onto %s. "
                    "Do not run any git commands; ask a question on the card." % (dep, base))
        if files:
            return ("The branch of %s changed; the board moved your work onto %s, but these files "
                    "still contain conflict markers: %s. Remove the markers by editing the files; do not commit."
                    % (dep, base, ", ".join(files)))
        return ("The branch of %s changed; the board moved your work onto %s. Continue." % (dep, base))
    if reason == "interrupted":
        return ("Your previous turn was interrupted by a server restart. Check the actual state of the "
                "worktree and the card, then continue.")
    if reason == "escalation":
        if r.get("from"):
            ref, who = r["from"], "task agent %s" % r["from"]
        else:
            ref, who = "<KEY>", "an unknown task (find it with `%s events --since <n>` or `%s thread _project`)" % (cli, cli)
        answer = " Answer it with `%s say %s --text ...`." % (cli, ref)
        if r.get("text"):
            return "Escalation from %s, verbatim:\n%s\n%s" % (who, _quote(str(r["text"])), answer.strip())
        return "Escalation from %s. " % who + _read_hint("%s thread _project" % cli, "Its text") + answer
    if reason == "proposal_rejected":
        if r.get("id"):
            return "The user rejected your proposal %s. Do not repeat it; adapt." % r["id"]
        return "The user rejected one of your proposals. " + _read_hint("%s thread _project" % cli, "Which one") + \
            " Do not repeat it; adapt."
    if reason == "unblocked":
        return "The task is unblocked: its dependencies are done or the external blocker was removed. Start working."
    if reason in ("ready", "manual"):
        return "Start working on the task."
    return "Woken: %s." % (reason or "unknown reason")


def wake_message(reasons: List[dict], digest: str, board_cli: str = "agent-board") -> str:
    """Одно сообщение из накопленных пробуждений, в порядке поступления.

    Ключи reason-словаря (все необязательны; при отсутствии текст отсылает агента к доске):
      message -> message_id, text; answer -> question_id, question, choice, comment;
      returned -> from; rebase -> from, base, rebase_ok, conflict_files, merge_branches, carried_ref;
      escalation -> from, text; proposal_rejected -> id; interrupted, unblocked, ready, manual -> без полей.
    Ключ key (задача получателя) используется только в подсказках «прочитай на доске».
    """
    out = "\n\n".join(_wake_line(r, board_cli) for r in reasons)
    if digest and digest.strip():
        out += "\n\n## Board changes since your last turn\n" + digest.strip()
    return out + "\n"


def _event_line(e: Event) -> Optional[str]:
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
