from __future__ import annotations

from dataclasses import MISSING, fields
from typing import List

from . import harness, ship, tracker
from .config import HARNESS_TYPES, NOT_SETTABLE, REVIEW_TOOLS, ProjectConfig

# описания полей для `config schema`: их читает скилл настройки, поэтому по-английски
DESCRIPTIONS = {
    "title": "Project title shown on the board.",
    "chains": "Start a dependent task once its dependency is approved, from the dependency branch; "
              "false: only after it is done.",
    "auto_approve": "Approve a code task as soon as its agent moves it to in_review, with no review round: "
                    "commit, rebase of dependents and ship.on_approve as on user approval; research tasks are "
                    "not auto-approved.",
    "max_agents": "Max concurrent task agent turns in this project; the project agent is not counted.",
    "global_max_agents": "Max concurrent task agent turns on the whole server; project agents run on top of it, one turn per project.",
    "project_agent_status": "Let the project agent change task statuses itself at the user's direct request "
                            "(done, in_progress, not_started, cancelled, external blocker); approval stays with the "
                            "user. false: it only proposes, the user applies with a button.",
    "project_agent_tasks": "Let the project agent create a task itself at the user's direct request; tasks it wants on "
                           "its own initiative still go through a proposal.",
    "archived": "Project is in the archive on the home screen; it opens and works as usual.",
    "language": "Language of the UI and agent messages (e.g. ru, en).",
    "repos": "Project repositories: name, absolute path to a git work tree, base branch.",
    "tracker": "Issue tracker for creating issues and reading their statuses; secrets only by env var name or "
               "file path.",
    "review": "Code review tool for review rounds of finished tasks.",
    "ship": "Command that sends an approved task branch to review (push, PR/MR); runs on user request, "
            "or right after approval with on_approve.",
    "resources": "Shared resources with exclusive access for `agent-board lock-run` (test stands, functional tests).",
    "agent_rules": "Extra rules added to every task agent and project agent prompt (links to CLAUDE.md/AGENTS.md, "
                   "build and test commands).",
    "harness": "Agent CLI that runs background sessions and its permission mode.",
    "branch_template": "Task branch name template.",
    "project_brief": "Project description for the project agent: goal, key decisions, constraints.",
    "links": "Project links shown in the UI (plan, spec, mockups).",
    "decomposition_rules": "Rules every task of the decomposition must follow (size, one repo per task, tests); "
                           "the project agent checks tasks against them.",
}

_TYPES = {"str": "string", "int": "integer", "bool": "boolean", "dict": "object", "List[dict]": "array"}


def _shapes() -> dict:
    variants = {
        "none": {"required": [], "optional": [], "auth_types": [], "example": {"type": "none"}},
        "jira": {
            "required": list(tracker.JIRA_REQUIRED),
            "optional": sorted(tracker.JIRA_KEYS - set(tracker.JIRA_REQUIRED) - {"type"}),
            "auth_types": list(tracker.AUTH_TYPES["jira"]),
            "epic": "Jira Server/DC: set epic_field to the Epic Link custom field id; without it the epic is sent "
                    "as parent (Jira Cloud).",
            "example": {"type": "jira", "url": "https://jira.example.com", "project": "PRJ", "issue_type": "Task",
                        "auth": {"type": "token_env", "env": "JIRA_TOKEN"}},
        },
        "github": {
            "required": list(tracker.GITHUB_REQUIRED),
            "optional": sorted(tracker.GITHUB_KEYS - set(tracker.GITHUB_REQUIRED) - {"type"}),
            "auth_types": list(tracker.AUTH_TYPES["github"]),
            "example": {"type": "github", "repo": "owner/name", "auth": {"type": "token_env", "env": "GITHUB_TOKEN"}},
        },
    }
    auth_examples = {"token_env": {"type": "token_env", "env": "TRACKER_TOKEN"},
                     "cookie_file": {"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"}}
    auth_notes = {"token_env": "env: name of the server env var with the token; user_env (Jira): env var with the "
                               "login, switches to Basic auth.",
                  "cookie_file": "path: file with the Cookie header value (Jira only)."}
    return {
        "repos": {"item": {"name": "unique repo name", "path": "absolute path to a git work tree",
                           "base": "base branch or null"},
                  "required": ["name", "path"],
                  "example": {"name": "api", "path": "/abs/path/api", "base": "main"}},
        "tracker": {
            "types": list(tracker.TYPES),
            "variants": variants,
            "auth": {kind: {"keys": list(keys), "note": auth_notes[kind], "example": auth_examples[kind]}
                     for kind, keys in tracker.AUTH_KEYS.items()},
            "statuses": {"targets": list(tracker.SYNC_TARGETS),
                         "note": "Map tracker status name -> board status; null = known status, no transition; "
                                 "empty map = statuses only shown. Approval is never mapped: only the user approves."},
            "title_placeholders": list(tracker.TITLE_PLACEHOLDERS),
            "default_poll_seconds": dict(tracker.DEFAULT_POLL_SECONDS),
        },
        "review": {"tool": {"enum": list(REVIEW_TOOLS)}, "binary": "review CLI binary, default crit"},
        "ship": {
            "keys": {"command": "shell command template or null (shipping disabled)",
                     "timeout": "seconds, default %g" % ship.DEFAULT_TIMEOUT,
                     "on_approve": "true: queue the command right after the approval commit (button or review round), "
                                   "as `agent-board ship <KEY>` would; default false; requires command",
                     "on_failure": "stay (default): a failed command keeps the task approved and only posts to the "
                                   "thread; return: a non-zero exit (not a timeout or cancel) moves the task back to "
                                   "in_progress, moves its branch onto the fresh base and wakes the agent with the "
                                   "command output to resolve conflicts, run tests and return the task to review"},
            "placeholders": list(ship.PLACEHOLDERS),
            "rules": ["Placeholder values are shell-escaped already: never put a placeholder inside quotes.",
                      "ANSI-C quotes $'...' are not supported.",
                      "Heredocs (<<) are not supported: move long text into a script.",
                      "{base} is the branch of the only unfinished dependency, else the repo base branch.",
                      "{tracker_key} and {tracker_url} are the linked tracker issue, empty without a link; also in "
                      "env AGENT_BOARD_TRACKER_KEY and AGENT_BOARD_TRACKER_URL."],
            "example": "gh pr create --base {base} --head {branch} --title {title} --fill",
        },
        "harness": {
            "type": {"enum": list(HARNESS_TYPES)},
            "permission_mode": {"for": "claude", "default": "auto", "known": list(harness.CLAUDE_PERMISSION_MODES)},
            "sandbox": {"for": "codex", "default": "workspace-write", "known": list(harness.CODEX_SANDBOXES),
                        "note": "network access is always enabled so the agent can reach the board"},
            "model": "optional model name",
            "allowed_tools": "claude only: extra allowed tools, list of strings",
            "note": "Background agents cannot ask for permission interactively: the mode decides what they may do "
                    "unattended.",
        },
        "resources": {"item": {"name": "unique; letters, digits, '-', '_', '.'",
                               "description": "how and when to use it"},
                      "required": ["name"],
                      "example": {"name": "functional-tests", "description": "Run with make ftest"}},
        "links": {"item": {"title": "link text", "href": "http(s) URL or file path"},
                  "required": ["title", "href"],
                  "example": {"title": "Spec", "href": "https://example.com/spec"}},
        "branch_template": {"placeholders": ["key"]},
    }


def config_schema() -> dict:
    """Описание настроек проекта, которые задаёт пользователь: тип, значение по умолчанию и допустимые значения."""
    shapes = _shapes()
    items = []
    for f in fields(ProjectConfig):
        if f.name in NOT_SETTABLE:
            continue
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            default = None
        item = {"name": f.name, "type": _TYPES[str(f.type)], "default": default,
                "description": DESCRIPTIONS[f.name]}
        if f.name in shapes:
            item["shape"] = shapes[f.name]
        items.append(item)
    return {"fields": items, "not_settable": sorted(NOT_SETTABLE)}


def _values(item: dict) -> str:
    shape = item.get("shape") or {}
    if item["name"] == "tracker":
        return "type: " + "|".join(shape["types"]) + "; statuses -> " + "|".join(shape["statuses"]["targets"])
    if item["name"] == "harness":
        return "type: " + "|".join(shape["type"]["enum"])
    if item["name"] == "review":
        return "tool: " + "|".join(shape["tool"]["enum"])
    if item["name"] == "ship":
        return "placeholders: " + " ".join("{%s}" % p for p in shape["placeholders"])
    return ""


def format_schema(schema: dict) -> str:
    import json

    rows = [(i["name"], i["type"], json.dumps(i["default"], ensure_ascii=False), _values(i) or i["description"])
            for i in schema["fields"]]
    head = ("field", "type", "default", "values / description")
    widths = [max(len(r[n]) for r in rows + [head]) for n in range(3)]
    lines: List[str] = []
    for row in [head] + rows:
        lines.append("  ".join(row[n].ljust(widths[n]) for n in range(3)) + "  " + row[3])
    lines.insert(1, "-" * len(lines[0]))
    return "\n".join(lines)
