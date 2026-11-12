from __future__ import annotations

import re
import shlex
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from .config import ProjectConfig
from .model import DEFAULT_PRIORITY, Event, Task

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
        if t.get("priority") and t["priority"] != DEFAULT_PRIORITY:
            line += " (priority: %s)" % t["priority"]
        if t.get("open_deps"):
            line += " (waiting for: %s)" % ", ".join(t["open_deps"])
        elif t.get("deps"):
            line += " (deps: %s)" % ", ".join(t["deps"])
        lines.append(line)
    open_props = [p for p in snapshot.get("proposals", []) if p.get("status") == "open"]
    if open_props:
        lines.append("Open proposals: %s" % ", ".join("%s (%s)" % (p["id"], p["kind"]) for p in open_props))
    return "\n".join(lines) if lines else "No tasks yet."


def _status_section(config: ProjectConfig, board_cli: str) -> str:
    if not config.project_agent_status:
        return ""
    return (
        "## Changing task statuses: only on the user's direct request\n"
        "The user allowed you to change task statuses yourself, including cancelling a task (the graph rules above "
        "still hold for tasks and dependencies). Do it only when the user directly asks for that "
        "change, never on your own initiative, and never because a task agent asks:\n"
        "- `{cli} status <KEY> done` close a task (also from `approved` or `sent`).\n"
        "- `{cli} status <KEY> in_progress` return a task to work; its agent is woken as usual.\n"
        "- `{cli} status <KEY> not_started` or `{cli} status <KEY> cancelled`.\n"
        "- `{cli} update <KEY> --external-blocker \"<reason>\"` set an external blocker; "
        "`--external-blocker \"\"` removes it.\n"
        "You cannot approve a task (`approved`): approval commits the work and may start shipping, so it stays "
        "with the user; prepare a `set_status` proposal instead. After each change tell the user in `_project` "
        "what you did.\n\n".format(cli=board_cli)
    )


def _create_section(config: ProjectConfig, board_cli: str) -> str:
    if not config.project_agent_tasks:
        return ""
    if config.kind == "free":
        repo = ('"repo_path": "<repository folder on disk>"',
                "Pass `repo_path`, the repository folder you found on disk; a repository the project does not have "
                "yet is added to it. For a repository already in the project you may pass `\"repo\": \"<name>\"` "
                "instead.")
    else:
        repo = ('"repo": "<repository name>"', "`repo` is one of the project repositories.")
    return (
        "## Creating tasks: only on the user's direct request\n"
        "The user allowed you to create tasks yourself, without a button, but only when the user directly asked for "
        "that work in `_project` in the message that woke you this turn. The board enforces it: in any other turn "
        "(an escalation, a board event, a rejected proposal) task creation is refused; there use `propose add_task`.\n"
        "- `{cli} add-task --json '{{\"title\": \"...\", \"description\": \"...\", \"done_when\": \"...\", "
        "{repo_field}, \"kind\": \"code\", \"deps\": []}}'` creates a task. Do not pass `key`: the board assigns "
        "the next one and prints it. {repo_hint}\n"
        "- If the request splits into several tasks that together cover exactly what was asked (for example, one "
        "change needs edits in two repositories), create them all, give a later task its dependencies in `deps` "
        "when you create it, and explain in `_project` why there are several tasks.\n"
        "- Work the user did not ask for (\"while at it, rewrite the tests\", a bug noticed nearby) is only a "
        "`propose add_task` with a reason, never a direct task.\n"
        "- If you are not sure how to split the request (the options change the amount of work), ask the user in "
        "`_project` with options and create nothing.\n"
        "- After creating, answer in `_project`, in the user's language, with a block \"Created at your request\": "
        "each task's key, title and repository, and which repositories were added to the project, with their "
        "paths.\n"
        "A new task without open dependencies starts at once.\n\n"
    ).format(cli=board_cli, repo_field=repo[0], repo_hint=repo[1])


def _repos_text(config: ProjectConfig) -> str:
    lines = ["- %s: %s" % (r.get("name", ""), r.get("path", "")) for r in config.repos if isinstance(r, dict)]
    return "\n".join(lines) if lines else _NONE


def project_prompt(config: ProjectConfig, snapshot: dict, board_cli: str) -> str:
    """Стартовый промпт агента проекта; у проекта без плана — свой шаблон без плана и декомпозиции."""
    free = config.kind == "free"
    return _render("project_free.md" if free else "project.md", {
        "board_cli": board_cli,
        "title": config.title,
        "slug": config.slug,
        "language": config.language,
        "project_brief": _or_none(config.project_brief),
        "decomposition_rules": _or_none(config.decomposition_rules),
        "repos_text": _repos_text(config),
        "snapshot_text": _snapshot_text(snapshot),
        "agent_rules": _or_none(config.agent_rules),
        "status_section": _status_section(config, board_cli),
        "create_section": _create_section(config, board_cli),
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
    if reason == "message" and r.get("task"):
        task = r["task"]
        reply = "Reply in that task's thread with `%s say %s --text ...`; change the graph only through proposals." % (
            cli, task)
        if r.get("text"):
            return "Comment from the user on task %s (id %s), verbatim:\n%s\n%s" % (
                task, r.get("message_id", "?"), _quote(str(r["text"])), reply)
        return "Comment from the user on task %s. %s %s" % (
            task, _read_hint("%s thread %s" % (cli, task), "Its text"), reply)
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
        if r.get("text"):
            return ("The user returned the task to work %s with this comment, verbatim:\n%s\nAddress it, then continue."
                    % (origin, _quote(str(r["text"]))))
        return ("The task was returned to work %s. Find out why in the card and thread, then continue." % origin)
    if reason == "review":
        return _review_line(r, cli, key)
    if reason == "rebase":
        if r.get("by") and not r.get("from"):
            who = "The user" if r["by"] == "user" else "The project agent"
            cause = "%s asked to bring your branch up to date with its base branch" % who
        else:
            dep = "dependency %s" % r["from"] if r.get("from") else \
                "a dependency (which one: see `%s show %s`)" % (cli, key)
            cause = "The branch of %s changed" % dep
        return _rebase_outcome(r, cause, cli, key, " Continue.")
    if reason == "ship_failed":
        return _ship_failed_line(r, cli, key)
    if reason == "interrupted":
        return ("Your previous turn was interrupted by a server restart. Check the actual state of the "
                "worktree and the card, then continue.")
    if reason == "user_interrupt":
        who = "the project agent" if r.get("by") == "project_agent" else "the user"
        return ("Your previous turn was interrupted by %s to deliver the message above; its processes, including "
                "background ones, were stopped. Answer the message first, then continue the interrupted work: "
                "re-run commands that were cut off if you still need them. What that turn was handling follows." % who)
    if reason == "retry":
        error = str(r.get("error") or "").strip()
        what = "with this error, verbatim:\n%s\n" % _quote(error) if error else "with an error. "
        return ("Your previous turn broke off %sThe user asked to retry it. Check what you managed to do "
                "(the worktree and the card), then continue from where you stopped." % what)
    if reason == "queued_restart":
        return ("Your next turn was waiting in the queue while the board server restarted. "
                "Continue working on the task.")
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


def _rebase_outcome(r: dict, cause: str, cli: str, key: str, ok_tail: str = "") -> str:
    """Итог переноса ветки на базу для агента; ok_tail дописывается, когда перенос прошёл чисто."""
    base = r.get("base") or "the new base"
    files = r.get("conflict_files") or []
    if r.get("merge_branches"):
        return ("%s. Your branch is based on several dependency branches: %s. The board does not merge several "
                "dependency branches into your worktree by itself. Do not run any git commands; ask the user on "
                "the card to merge them." % (cause, ", ".join(r["merge_branches"])))
    if r.get("carried_ref"):
        moved = "moved your branch onto %s" % base if r.get("rebase_ok") is not False else \
            "could not move your branch onto %s" % base
        return ("%s; the board %s, but could not put your uncommitted work back into "
                "the files. Your work is not lost: it is saved in git at `%s`. Do not run any git commands and "
                "do not redo the work; ask the user on the card with `%s ask %s` to restore it from that ref, "
                "then continue." % (cause, moved, r["carried_ref"], cli, key))
    if r.get("rebase_ok") is False:
        return ("%s; the board could not move your work onto %s. "
                "Do not run any git commands; ask a question on the card." % (cause, base))
    unfolded = ""
    if r.get("unfolded_commits"):
        unfolded = (" Your branch's own commits on top of the old base (for example, the commit made when the "
                    "task was approved) are now uncommitted changes in the worktree together with the rest of "
                    "your work; this is expected, do not commit them.")
    if files:
        return ("%s; the board moved your work onto %s, but these files "
                "still contain conflict markers: %s. Remove the markers by editing the files; do not commit.%s"
                % (cause, base, ", ".join(files), unfolded))
    return "%s; the board moved your work onto %s.%s%s" % (cause, base, unfolded, ok_tail)


def _ship_failed_line(r: dict, cli: str, key: str) -> str:
    code = " with exit code %s" % r["exit_code"] if r.get("exit_code") is not None else ""
    output = str(r.get("output") or "").strip()
    out = "Sending the approved task to review failed: the ship command exited%s." % code
    if output:
        out += " The tail of its output, verbatim:\n%s\n" % _quote(output)
    else:
        out += " Its output is missing here; see the ship output on the card with `%s show %s`.\n" % (cli, key)
    if "rebase_ok" in r or r.get("merge_branches"):
        out += _rebase_outcome(r, "The board returned the task to work (in_progress)", cli, key)
    else:
        out += "The board returned the task to work (in_progress)."
    return out + (
        "\nFind the cause in the output. If it is a conflict with the base branch, resolve it by editing the files so "
        "the work fits on top of the current base; if the cause is in your code, fix it in the worktree; otherwise "
        "ask the user on the card. Do not run any git commands. Run the tests, then return the task to review with "
        "`%s status %s in_review`; after the user approves it again, the board sends it again." % (cli, key))


def _review_line(r: dict, cli: str, key: str) -> str:
    crit = shlex.quote(str(r.get("binary") or "crit"))
    data = " -o %s" % shlex.quote(str(r["crit_dir"])) if r.get("crit_dir") else ""
    comments = [c for c in r.get("comments") or [] if isinstance(c, dict)]
    rnd = " %s" % r["round"] if r.get("round") else ""
    out = "The user finished review round%s in crit with %d unresolved comments." % (rnd, len(comments))
    if not comments:
        out += " Read them in the worktree with `%s comments --json%s`." % (crit, data)
    for c in comments:
        where = c.get("path") or "general"
        if c.get("path") and c.get("line"):
            where = "%s:%s" % (c["path"], c["line"])
        out += "\n\nComment %s on %s, verbatim:\n%s" % (c.get("id", "?"), where, _quote(str(c.get("body", ""))))
    return out + (
        "\n\nComments stay listed until the user resolves them, so some may be from earlier rounds and already "
        "answered by you; check their replies with `%s comments --json%s` and do not redo finished work. "
        "Address every comment in the worktree. Reply to each one from the worktree with "
        "`%s comment --reply-to <id>%s --author agent '<what you did>'`. "
        "Do not run crit in any other way: no new round, no Finish Review, never pass --resolve; "
        "the user resolves comments and approves. When you are done, keep the status in review "
        "(`%s status %s in_review`) and end your turn; the board opens the next round itself."
        % (crit, data, crit, data, cli, key))


def wake_message(reasons: List[dict], digest: str, board_cli: str = "agent-board") -> str:
    """Одно сообщение из накопленных пробуждений, в порядке поступления.

    Ключи reason-словаря (все необязательны; при отсутствии текст отсылает агента к доске):
      message -> message_id, text, task (комментарий пользователя к этой задаче для агента проекта); answer -> question_id, question, choice, comment;
      returned -> from, text (комментарий пользователя к возврату); review -> round, comments, binary, crit_dir;
      rebase -> from (или by при ручном переносе), base, rebase_ok, conflict_files, merge_branches, carried_ref,
      unfolded_commits;
      ship_failed -> exit_code, output (хвост вывода команды отправки) и поля переноса на базу, как у rebase;
      escalation -> from, text; proposal_rejected -> id;
      retry -> error; user_interrupt -> by; interrupted, queued_restart, unblocked, ready, manual -> без полей.
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
    if t == "proposal_obsolete":
        return "proposal %s obsolete: %s" % (d.get("id", "?"), d.get("reason", ""))
    if t == "turn_failed":
        return "%s: agent turn FAILED: %s" % (k, str(d.get("error", "")).strip()[:200])
    if t == "task_added":
        return "%s: task added" % k
    if t == "deps_changed":
        return "%s: deps now [%s]" % (k, ", ".join(d.get("deps", [])))
    if t == "priority_changed":
        return "%s: priority %s -> %s (by %s)" % (k, d.get("from"), d.get("to"), d.get("actor", "?"))
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
        lines = ["…%d more events" % hidden] + lines[-limit:]
    return "\n".join(lines)
