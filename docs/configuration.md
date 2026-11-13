# Settings

Settings live on two levels:

- **Board settings** — one file `<home>/settings.json` for the whole board: the language, the server-wide agent limit
  and the defaults for the agent CLI and the review tool. See [Board settings](#board-settings).
- **Project settings** — `<home>/projects/<slug>/project.json` per project. A project inherits `harness` and `review`
  from the board unless it sets its own value. See [Inheritance](#inheritance).

The authoritative list of fields, with types, defaults and allowed values, is printed by the board itself:

```bash
agent-board config schema          # project fields; the same as GET /api/p/<slug>/config/schema
agent-board settings schema        # board fields; the same as GET /api/settings/schema
```

## Board settings

| Field | Default | Purpose |
|---|---|---|
| `language` | `ru` | Language of the web UI (Russian for `ru`, English for anything else) and the language agents use with the user: summaries, questions, thread messages, reports. The board's own system messages in threads are written in it too; messages already saved are not rewritten when it changes. |
| `global_max_agents` | `6` | Concurrent task agent turns on the whole server. Project agents run on top of it, one turn per project. |
| `harness` | `{"type": "claude", "permission_mode": "auto"}` | The agent CLI for projects that do not set their own, see [Harness](#harness). |
| `review` | `{"tool": "crit", "binary": "crit"}` | The review tool for projects that do not set their own: `crit` or `none`, see [Review](review-and-ship.md#review). |

`language` and `global_max_agents` exist only here: a project cannot set them, and `config set` (or a `set_config`
proposal) with them is rejected with a hint to use `settings set`.

Board settings are changed by the user only:

- `agent-board settings set --json '{"language": "en"}'` (add `--dry-run` to only check); `agent-board settings get`
  (alias `show`) prints them;
- the "Board settings" window: the gear in the home screen header. Language is a list (Русский, English), the
  agent limit is a number; the default agent is the CLI (Claude Code, Codex), the model (empty — the CLI default) and
  the permission mode for Claude Code or the sandbox for Codex; the default review is the tool and its command. Other
  keys of `harness` (for example `allowed_tools`) are kept. Errors are shown next to the field;
- `PUT /api/settings {"actor": "user", "fields": {...}, "dry_run": false}`.

Values are checked as in `config set`; an unknown field is rejected. Each top-level field is replaced whole. After a
change every project in which the changed field is in effect (all projects for `language` and `global_max_agents`,
projects without their own value for `harness` and `review`) gets a `config_changed {fields, settings: true}` event,
so open pages and the runner pick it up at once: raising `global_max_agents` starts queued turns right away. The
common stream `/api/stream` also gets an item `{"slug": null, "type": "settings_changed", "fields": [...]}`. A field
broken by hand in the file falls back to its default.

### Moving from per-project settings

Earlier versions kept `language` and `global_max_agents` (and full `harness` and `review`) in every project. When the
board starts without `settings.json` (or first reads its settings), it creates the file from those projects:

- `global_max_agents` — the largest among the projects;
- `language`, `harness`, `review` — the value most projects have (on a tie, the one of the project that comes first
  by name); a project without the field counts with the old default;
- `language` and `global_max_agents` are removed from the projects; `harness` and `review` equal to the board value
  stop being project overrides, the others stay overrides.

A board without projects gets the defaults. Once `settings.json` exists the move never runs again, and later changes
to the board settings are not touched.

## Inheritance

`harness` and `review` in `project.json` are either an object (the project's own value, an override) or `null` /
absent (the board value applies). The effective settings — `GET /api/p/<slug>/config`, `config` in the project
snapshot (`/state`), `agent-board config get` — already contain the resulting values, plus:

- `overrides` — the list of inherited fields the project sets itself, for example `["harness"]`;
- `language` and `global_max_agents` — the board values, read-only.

Setting `harness` or `review` with `config set` makes it an override, even if it equals the board value. To drop an
override and return to the board value, set the field to `null`:
`agent-board --project <slug> config set --json '{"harness": null}'`. Other changes to project settings keep inherited
fields inherited.

In the "Project settings" window `harness` and `review` are marked "Same as the board" or "Own value". Editing the
field makes it an own value; "Reset to the board" puts the board value back into the editor ("Becomes the board value
after saving"), and saving sends `null`. `language` and `global_max_agents` are not in this window — they are in the
board settings.

A new project ([`POST /api/projects`](api.md#new-project), `init`) inherits everything; the agent chosen in the
"New project" dialog becomes an override only if it differs from the board's agent.

## Changing project settings

Project settings are changed by the user only, in one of three ways:

- `agent-board --project <slug> config set --json '{"max_agents": 2}'` (add `--dry-run` to only check);
- the "Project settings" window: the gear next to the project name in the header. Boolean fields are toggles,
  numbers and text are inputs, structured fields are JSON editors; the ship command has its own editor with the
  list of placeholders. A dry run runs before saving, and errors are shown next to the field;
- `POST /api/p/<slug>/config {"actor": "user", "fields": {...}, "dry_run": false}`.

On save the changed fields are fully checked: the ship template, tracker keys and the kind of `auth`, status
targets, links, repositories (an absolute path to an existing git work tree, unique names), harness, review tool
(or `null`), limits (integers ≥ 1), resources, `branch_template`. The directory and `git` are checked only for new and changed
repositories, so a repository removed from disk does not block other changes. Slow checks run before the board lock
is taken; under the lock the settings are re-read and checked for shape, and if the same field was changed elsewhere
at the same time the save is retried up to three times, then fails with 409.

`config validate` checks the whole saved file. An unknown field, `language` and `global_max_agents` (board settings),
`slug`, `kind` (set when the project is created),
`stage` (use `agent-board stage`) and `project_session_id` (maintained by the runner) are rejected with 400. A change writes a `config_changed {fields}`
event; raising `max_agents` starts queued turns right away.

The deprecated top-level `ship_command` field is moved into `ship.command` (if that is empty) the first time the
board reads the file, with a `config_migrated {field, to, moved}` event.

## Project fields

| Field | Default | Purpose |
|---|---|---|
| `title` | — | Project title shown on the board (set by `init`). |
| `repos` | `[]` | Project repositories: `[{"name", "path", "base"}]`. `path` is an absolute path to a git work tree, `base` the base branch (default `main`). |
| `branch_template` | `{key}` | Task branch name template; the only placeholder is `{key}`, and it is required. |
| `chains` | `true` | Start a dependent task as soon as its dependency is approved, from the dependency's branch. `false`: only after the dependency is done. |
| `max_agents` | `3` | Concurrent task agent turns in this project. The project agent is not counted. |
| `harness` | `null` — the board value | The agent CLI, see [Harness](#harness) and [Inheritance](#inheritance). |
| `review` | `null` — the board value | Review tool: `crit` or `none`, see [Review](review-and-ship.md#review) and [Inheritance](#inheritance). |
| `auto_approve` | `false` | Approve code tasks without review as soon as the agent moves them to `in_review`, see [Auto-approve](review-and-ship.md#auto-approve). |
| `ship` | `{"command": null, "timeout": 600, "on_approve": false, "on_failure": "stay"}` | The command that sends approved work to review, see [Shipping](review-and-ship.md#shipping). |
| `tracker` | `{"type": "none"}` | Jira or GitHub Issues, see [Trackers](trackers.md). |
| `resources` | `[]` | Shared resources for `lock-run`: `[{"name", "description"}]`; names use letters, digits, `.`, `_`, `-`. |
| `links` | `[]` | Project links in the UI: `[{"title", "href"}]`, see [Links](#links). |
| `agent_rules` | `""` | Extra rules (build and test commands, links to `CLAUDE.md`/`AGENTS.md`). They go into the starting prompt of every task agent and of the project agent. |
| `project_brief` | `""` | Project description for the project agent: goal, key decisions, constraints. |
| `decomposition_rules` | `""` | Rules every task must follow (up to 4000 characters). They go into the project agent's prompt, and it checks the task graph against them. |
| `project_agent_status` | `false` | Let the project agent change task statuses at the user's request, see [below](#project-agent-statuses). |
| `project_agent_tasks` | `true` | Let the project agent create a task itself at the user's direct request, see [below](#project-agent-tasks); tasks it wants on its own initiative still go through a [proposal](workflow.md#proposals). |
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

### Project agent tasks

With `project_agent_tasks: true` (default) the project agent creates tasks itself (`agent-board add-task`, without a
button) when the user directly asked for them in the "Project" thread. The board allows it only during a turn woken by
a user message; in a turn woken by an escalation of a task agent or by a board event the command is refused, and the
agent [proposes](workflow.md#proposals) `add_task` instead. Such a task has `created_by: "project_agent"`; tasks the
user adds, including through an applied proposal, have `created_by: "user"`. With `false` the project agent never
creates tasks, it only proposes them. The full rule is in [Tasks from the project agent](workflow.md#tasks-from-the-project-agent).
