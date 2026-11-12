# Project settings

Each project keeps its settings in `<home>/projects/<slug>/project.json`. The authoritative list of fields, with
types, defaults and allowed values, is printed by the board itself:

```bash
agent-board config schema          # table
agent-board config schema --json   # the same as GET /api/p/<slug>/config/schema
```

## Changing settings

Settings are changed by the user only, in one of three ways:

- `agent-board --project <slug> config set --json '{"max_agents": 2}'` (add `--dry-run` to only check);
- the "Project settings" window: the gear next to the project name in the header. Boolean fields are toggles,
  numbers and text are inputs, structured fields are JSON editors; the ship command has its own editor with the
  list of placeholders. A dry run runs before saving, and errors are shown next to the field;
- `POST /api/p/<slug>/config {"actor": "user", "fields": {...}, "dry_run": false}`.

On save the changed fields are fully checked: the ship template, tracker keys and the kind of `auth`, status
targets, links, repositories (an absolute path to an existing git work tree, unique names), harness, review tool,
limits (integers ≥ 1), resources, `branch_template`. The directory and `git` are checked only for new and changed
repositories, so a repository removed from disk does not block other changes. Slow checks run before the board lock
is taken; under the lock the settings are re-read and checked for shape, and if the same field was changed elsewhere
at the same time the save is retried up to three times, then fails with 409.

`config validate` checks the whole saved file. An unknown field, `slug`, `kind` (set when the project is created),
`stage` (use `agent-board stage`) and `project_session_id` (maintained by the runner) are rejected with 400. A change writes a `config_changed {fields}`
event; raising `max_agents` or `global_max_agents` starts queued turns right away.

The deprecated top-level `ship_command` field is moved into `ship.command` (if that is empty) the first time the
board reads the file, with a `config_migrated {field, to, moved}` event.

## Fields

| Field | Default | Purpose |
|---|---|---|
| `title` | — | Project title shown on the board (set by `init`). |
| `language` | `ru` | Language of the web UI (Russian for `ru`, English for anything else) and the language agents use with the user: summaries, questions, thread messages, reports. The board's own system messages in threads are written in it too; messages already saved are not rewritten when it changes. |
| `repos` | `[]` | Project repositories: `[{"name", "path", "base"}]`. `path` is an absolute path to a git work tree, `base` the base branch (default `main`). |
| `branch_template` | `{key}` | Task branch name template; the only placeholder is `{key}`, and it is required. |
| `chains` | `true` | Start a dependent task as soon as its dependency is approved, from the dependency's branch. `false`: only after the dependency is done. |
| `max_agents` | `3` | Concurrent task agent turns in this project. The project agent is not counted. |
| `global_max_agents` | `6` | Concurrent task agent turns on the whole server. Project agents run on top of it, one turn per project. |
| `harness` | `{"type": "claude", "permission_mode": "auto"}` | The agent CLI, see [Harness](#harness). |
| `review` | `{"tool": "crit", "binary": "crit"}` | Review tool: `crit` or `none`, see [Review](review-and-ship.md#review). |
| `auto_approve` | `false` | Approve code tasks without review as soon as the agent moves them to `in_review`, see [Auto-approve](review-and-ship.md#auto-approve). |
| `ship` | `{"command": null, "timeout": 600, "on_approve": false, "on_failure": "stay"}` | The command that sends approved work to review, see [Shipping](review-and-ship.md#shipping). |
| `tracker` | `{"type": "none"}` | Jira or GitHub Issues, see [Trackers](trackers.md). |
| `resources` | `[]` | Shared resources for `lock-run`: `[{"name", "description"}]`; names use letters, digits, `.`, `_`, `-`. |
| `links` | `[]` | Project links in the UI: `[{"title", "href"}]`, see [Links](#links). |
| `agent_rules` | `""` | Extra rules (build and test commands, links to `CLAUDE.md`/`AGENTS.md`). They go into the starting prompt of every task agent and of the project agent. |
| `project_brief` | `""` | Project description for the project agent: goal, key decisions, constraints. |
| `decomposition_rules` | `""` | Rules every task must follow (up to 4000 characters). They go into the project agent's prompt, and it checks the task graph against them. |
| `project_agent_status` | `false` | Let the project agent change task statuses at the user's request, see [below](#project-agent-statuses). |
| `project_agent_tasks` | `true` | Let the project agent create a task itself at the user's direct request; tasks it wants on its own initiative still go through a [proposal](workflow.md#proposals). |
| `archived` | `false` | The project is in the archive on the home screen. An archived project opens and works as usual. |
| `kind` | `plan` | Project type, set when the project is created and never changed: `plan` — tasks from a decomposition, stages from decomposition to completion; `free` — tasks one by one, the project starts at the `work` stage. Projects without the field are `plan`. Not settable. |

### Harness

| Key | Values |
|---|---|
| `type` | `claude` (default), `codex`, `fake` (tests only) |
| `permission_mode` | Claude: `auto` (default), `acceptEdits`, `bypassPermissions`, `dontAsk`, `manual`, `plan`; passed to `claude` as is |
| `allowed_tools` | Claude: extra allowed tools, for example `["Bash(python3 -m pytest:*)"]` |
| `sandbox` | Codex: `workspace-write` (default), `read-only`, `danger-full-access` |
| `model` | model name passed to the CLI |

Under `acceptEdits` agents cannot run tests or builds until those commands are allowed in `allowed_tools`; without
them the agent stops and asks a question. `auto` needs no such settings. Claude agents are always allowed the board
subcommands `status`, `update`, `ask`, `say`, `show`, `thread`, `state`, `events`, `propose`, and with crit review
also `crit comment` (to reply to review comments). Codex runs with
network access in its sandbox (`-c sandbox_workspace_write.network_access=true`): without it a sandboxed agent
cannot reach the local board. Choosing a mode is a security decision, see [Security](security.md).

Codex support has not been tested against a live `codex`: it is covered only by tests with a faked process.

### Links

`href` is an http(s) URL or a path to a local file: absolute, or relative to the first repository in `repos`
(without repositories, to the project directory `<home>/projects/<slug>`). The board serves a file through
`GET /api/p/<slug>/file?path=` only from inside the project directory, its repositories and the task worktrees, up
to 2 MB; HTML is served with `Content-Security-Policy: sandbox`. Other schemes (`javascript:`, `data:`, `file:`) are
rejected.

### Project agent statuses

With `project_agent_status: true` the project agent changes task statuses itself at the user's direct request:
`done` (also from `approved` and `sent`), `in_progress` (back to work; the task agent is woken as usual),
`not_started`, `cancelled`, and it sets or removes the external blocker. `approved`, `sent` and `in_review` stay
with the user: approval commits the work and may start shipping. Each change goes to the journal with
`actor: project_agent` and a system message in the task thread, and the project agent reports it in the "Project"
thread. With `false` (default) the board refuses, and the agent only [proposes](workflow.md#proposals).
