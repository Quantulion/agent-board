# Command-line tool

`agent-board` is both the server and the client. Run it as `agent-board …` after [installing](installation.md),
or as `PYTHONPATH=board python3 -m agent_board …` from the repository root.

Global option: `--project <slug>` (before the subcommand) selects the project; `AGENT_BOARD_PROJECT` sets the
default. All commands print JSON results unless stated otherwise.

## Board lifecycle

| Command | What it does |
|---|---|
| `open [--no-browser]` | Open the board as an app: start it in the background if it is not running (like `serve --detach`), open its home screen in the browser and print the link. If a board is already running it just opens it again; a running board of a different version exits with code 3. `--no-browser` only prints the link (remote machines, scripts). `agent-board` without a command does the same as `open`. |
| `serve [--detach] [--port N] [--no-runner] [--no-tracker-sync]` | Start the board together with the agent runner and prints its link. `--detach` runs it in the background with a log in `<home>/server.log`; if a board of the same version is already running it just prints that link, a different version exits with code 3. `--no-runner` (board only) and `--no-tracker-sync` (no background tracker sync) are for debugging. Without `--port` the board takes the first free port in 8737–8746, or a random one if all are busy. A second `serve` on the same data directory exits with code 3. |
| `stop` | Stop the running board: SIGTERM to the pid from `<home>/server.json` after checking that it is the board's process. No board running — code 0. |
| `status [--json]` | Without arguments: whether the board is running (link, version, pid) and the projects with their stages. Needs no server. With arguments (`status <KEY> <STATUS>`) it changes a task's status, see below. |
| `version` | Print the agent-board version. |
| `self-update [--ref REF] [--repo PATH]` | Reinstall the board from a commit, see [Installation](installation.md#updating). |
| `init <slug> --title T [--kind plan\|free]` | Create a project. `plan` (default): tasks come from a decomposition, the project starts at the decomposition stage. `free`: tasks are added one by one, the project starts at the `work` stage. The type does not change later. `global_max_agents` is copied from the existing projects (the largest one). |

## Tasks and threads

| Command | What it does |
|---|---|
| `state` | Full project snapshot (tasks, project agent, proposals, settings). |
| `show <KEY>` | A single task from the snapshot. |
| `status <KEY> <STATUS>` | Change a task's status. A task agent may set only `in_progress` and `in_review`; the project agent only with [`project_agent_status`](configuration.md#project-agent-statuses). |
| `update <KEY> [options]` | Update task fields, see the table below. |
| `add-task --json '{...}'` | Add a task. Fields: `key` (omit it to get the next key of the project), `title`, `description`, `done_when`, `repo` (a name from `repos`), `kind` (`code` or `research`), `estimate`, `deps`, `priority`. User only,* and the project agent at the user's direct request (see [Tasks from the project agent](workflow.md#tasks-from-the-project-agent)). |
| `deps <KEY> --json '[...]'` | Replace a task's dependencies with a list of keys. A cycle or an unknown task — code 1. User only.* |
| `ask <KEY> --text T [--option O ...] [--context C] [--recommended O]` | An agent asks the user a question; the task waits for the answer. |
| `say <THREAD> --text T [--interrupt]` | Post to a task thread or to `_project`. `--interrupt` stops the agent's running turn so it answers at once (user and project agent only), see [Interrupting a turn](workflow.md#interrupting-a-turn). |
| `thread <THREAD>` | Read a thread. |
| `events [--since N]` | The project's event log from event number `N`. |

`update` options:

| Option | Who may change it |
|---|---|
| `--summary`, `--todo-json`, `--changed-files-json`, `--report` | anyone (agents keep their card current with these) |
| `--title`, `--description`, `--done-when`, `--estimate N` | user only |
| `--priority high\|normal\|low` | user or project agent |
| `--rule-violations-json` | user or project agent |
| `--tracker-key K`, `--tracker-url U` | user only: links the task to an issue that already exists in the tracker (`PROJ-123` for Jira, `#123` for GitHub; the URL must be http(s)). An empty value removes the link. |
| `--external-blocker R` | user, or the project agent with `project_agent_status`. An empty value removes the blocker. |

## Agent turns

| Command | What it does |
|---|---|
| `retry <KEY\|_project>` | Retry a failed agent turn (agent in `error`). User only; see [Retrying](workflow.md#failures-and-retrying). |
| `rebase <KEY>` | Move the task branch onto the current base branch with its uncommitted work and wake the agent. User or project agent; see [Rebasing](workflow.md#rebasing). |
| `lock-run <resource> -- <command...>` | Run a command while holding an exclusive project resource (queued per resource, the queue survives a restart). Must be called from an agent (`AGENT_BOARD_TASK` set). |

## Project, review and shipping

| Command | What it does |
|---|---|
| `stage <stage>` | Move the project to a stage: `decomposition` → `tracker` (optional) → `work` → `done`. User only.* |
| `ship <KEY>...` | Send approved tasks to review with the project's command. User only.* |
| `tracker create\|check\|sync` | Create tasks in the tracker, check tracker access, or sync statuses now. User only.* On errors — code 1. |
| `propose <kind> --json '{...}' [--reason R]` | A project agent proposal; the user applies or rejects it, see [Proposals](workflow.md#proposals). |
| `config get [--json]` | Show project settings. User only.* |
| `config set --json '{...}' [--dry-run]` | Change settings fields with all save-time checks; `--dry-run` only checks. User only.* |
| `config validate` | Check the whole saved settings file. User only.* |
| `config schema [--json]` | List the settings fields with type, default, description and allowed values. Needs neither a board nor a project. |

\* User commands act as the user only when `AGENT_BOARD_ACTOR` is not set. The runner starts agents with it
(an empty value also counts as an agent), so agents are refused on user-only actions. This protects against
mistakes and is not a trust boundary, see [Security](security.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the request failed (validation, permission, tracker error) |
| 2 | no running board (or it did not answer in time) |
| 3 | another board is already running on this data directory, or a different version is running (`serve --detach`, `open`) |

All commands except `open`, `serve`, `init`, `stop`, `version`, `self-update`, `status` without arguments and
`config schema` talk to the running board.

## Environment variables

| Variable | Purpose |
|---|---|
| `AGENT_BOARD_HOME` | Data directory (default `~/.agent-board`). |
| `AGENT_BOARD_PROJECT` | Default project instead of `--project`. |
| `AGENT_BOARD_ACTOR` | Who acts: `agent`, `user`, `project_agent`; the runner sets it for agents. Not set: `say` and the user commands act as `user`, `status <KEY> <STATUS>` acts as `agent` (that command is mostly called by agents, while a message from the terminal is written by a human). |
| `AGENT_BOARD_TASK` | The agent's task, set by the runner. With it `say _project` wakes the project agent (escalation) and `lock-run` knows whose turn holds the resource. |
| `AGENT_BOARD_LANG` | Language of CLI messages and board errors: `en` (default) or `ru`. The runner sets it for agents from the project `language`. The CLI passes it to the board in the `X-Board-Lang` header. |
| `AGENT_BOARD_LOCK_POLL` | How often `lock-run` polls its place in the queue, seconds (default 2). |
| `AGENT_BOARD_FROM_REPO` | `1` makes the skill wrapper run the code from the repository instead of the installed version. |
| `AGENT_BOARD_TRACKER_KEY`, `AGENT_BOARD_TRACKER_URL` | Set for the ship command: the task's tracker issue, see [Shipping](review-and-ship.md#shipping). |
