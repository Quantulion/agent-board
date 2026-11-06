# agent-board

**English** · [Русский](README.ru.md)

A local task board for AI coding agents: a pure Python 3.9+ daemon (standard library only) keeps projects, tasks, dependencies, questions and conversations in files, serves an HTTP API with an event stream (SSE), and ships a command-line tool for agents.

Data lives in `$AGENT_BOARD_HOME` (default `~/.agent-board`).

## Running

From the repository root, without installing:

```bash
PYTHONPATH=board python3 -m agent_board init pay --title "Payments"   # create a project
PYTHONPATH=board python3 -m agent_board serve                        # start the board
```

Or install the package (`pip install -e .`) and use the `agent-board` command.

`serve` prints a link like `http://127.0.0.1:8737/?token=...`. The token is stored in `<home>/token` and stays the same across restarts; the default port is the first free one in 8737–8746 (`--port N` sets it explicitly). Only one board runs per data directory: a second `serve` exits with code 3.

## Installing and updating

When the board runs straight from the repository checkout, every uncommitted edit lands in the running board at once, and conflict markers left by a merge break the CLI for every agent. To avoid that, install the board from a fixed commit:

```bash
python3 scripts/install.py            # the main branch HEAD (main, else master, else HEAD)
python3 scripts/install.py --ref v1   # a specific commit, branch or tag; --repo PATH, --home DIR
```

The script copies `board/agent_board` from the commit (via `git archive`, so the working tree does not matter) into `<home>/runtime/<commit>` and atomically switches the wrapper `<home>/bin/agent-board` to it; add `<home>/bin` to `PATH`. The wrapper ignores the current directory and the repository state, and uses the Python interpreter the script was run with. The skill wrapper (`skills/agent-board/scripts/agent-board`) also prefers the installed version when it exists; `AGENT_BOARD_FROM_REPO=1` makes it run the code from the repository instead.

Update to a new commit:

```bash
agent-board self-update               # the same repository and ref as the last install
agent-board self-update --ref <sha>   # another commit, or back to an earlier one
```

The running server keeps the code it was started with; after an update restart it: `agent-board stop && agent-board serve --detach`. Old `<home>/runtime/<commit>` directories are kept (a running server may still use one) and can be deleted by hand.

## Installing the skill

The `skills/agent-board` skill lays a plan out on the board and starts the board. It needs python3 ≥ 3.9 and git; [crit](https://github.com/tomasz-tomczyk/crit) is optional, for reviews.

**Claude Code** (plugin from a local marketplace):

```
/plugin marketplace add ~/path/to/agent-board   # absolute path or ~
/plugin install agent-board@agent-board-local
```

**Codex** (a symlink in `${CODEX_HOME:-~/.codex}/skills`, no copies):

```bash
cd /path/to/agent-board                  # run the scripts from the repository root
sh scripts/install-codex.sh              # install; --force replaces a foreign symlink and moves a directory aside to .bak.<timestamp>
sh scripts/install-codex.sh --uninstall  # remove the symlink
```

Harness specifics: `skills/agent-board/references/harness-claude.md` and `harness-codex.md`.

## Usage

- After a planning skill (for example, writing-plans), tell the agent "lay the plan out on the board": the skill starts the board, creates and configures a project, turns the plan into tasks with dependencies, checks the decomposition rules and gives you a link to the decomposition page.
- "Start the board for project X" — the skill starts the board and gives you a link to the project.
- From then on everything happens on the board: the project agent reads comments on tasks, "Approve decomposition" starts the task agents, and creating tasks in the tracker and sending work to review are user buttons. You no longer need the terminal.

## Features and limitations

| Feature | Status |
|---|---|
| Projects, tasks, dependencies, agent questions, conversations, SSE events | available |
| Laying a plan out with the skill, decomposition rule checks, decomposition approval | available (verified live with Claude Code) |
| Task agents in separate worktrees, concurrency limits, dependent tasks start from merged dependencies | available |
| Project agent (comments on tasks and on the decomposition, proposals) | available |
| Review via crit, commit on approval | available, crit is optional |
| Sending to review with the project's command, tracker sync (Jira, GitHub Issues) | available |
| Web UI: board, graph, task card, decomposition | available |
| Harnesses | Claude Code; Codex (see limitations); `fake` for tests |

Limitations:

- Codex support has not been tested against a live `codex`: it is covered only by tests with a faked process.
- The web UI is in Russian only. Agents talk to you in the language set by the project's `language` setting (default `ru`).
- Under `permission_mode: acceptEdits` agents cannot run tests or builds until those commands are allowed in `harness.allowed_tools` (for example, `["Bash(python3 -m pytest:*)"]`); without it the agent stops and asks a question. The `auto` mode needs no such settings.
- The board listens only on the local address and is meant for a single user; the access token lives in `<home>/token`.
- The board makes the commit on approval, and the push happens only when the user sends work to review; agents are never asked to do either, and the board does not accept approval or sending on behalf of an agent.
- Trust boundary: separating the user from the agents protects against mistakes, not against an agent deliberately acting as the user. The actor is determined by the `AGENT_BOARD_ACTOR` variable and the `actor` field of a request, while agents run as the same OS user and can read `<home>/token`. An agent with unrestricted shell access (`permission_mode: auto` or `bypassPermissions`) can run a command without `AGENT_BOARD_ACTOR` or call the API directly and perform user actions: approving, sending to review, changing the stage, changing settings, committing and pushing with git. If this matters to you, use `acceptEdits` with a narrow `harness.allowed_tools`, or the Codex sandbox.
- The link with the token (`?token=…`) printed by `serve` and by the skill ends up in the chat history; the token does not change across restarts. The risk is small (the board listens only locally, and the token file is readable by the same user anyway), but do not publish those logs. To rotate the token: stop the board, delete `<home>/token`, start it again.
- Trackers: Jira and GitHub Issues; the tracker is disabled by default.

## Commands

| Command | What it does |
|---|---|
| `serve [--detach] [--port N] [--no-runner] [--no-tracker-sync]` | start the board together with the agent runner (`--no-runner` — board only, `--no-tracker-sync` — no background tracker sync; both for debugging; `--detach` — run in the background with a log in `<home>/server.log`: prints the link; if a board of the same version is already running, just prints its link; a different version — code 3) |
| `stop` | stop the running board (SIGTERM to the pid from `server.json` after checking it is the board's process; no board — code 0) |
| `version` | agent-board version |
| `status [--json]` | without arguments: whether the board is running (link, version, pid) and the projects with their stages; no server needed. The `status <KEY> <STATUS>` form changes a task's status |
| `init <slug> --title T` | create a project |
| `state` | full project snapshot |
| `show <key>` | a single task |
| `status <key> <status>` | change a task's status |
| `update <key> [--summary ...] [--todo-json ...] [--changed-files-json ...] [--report ...] [--rule-violations-json ...] [--title ...] [--description ...] [--done-when ...] [--estimate N] [--tracker-key K] [--tracker-url U]` | update task fields; without `AGENT_BOARD_ACTOR` the command acts as the user; the wording (`title`, `description`, `done_when`, `estimate`) and the link to an already existing tracker issue (`tracker_key` — in the project's tracker format: Jira `PROJ-123`, GitHub `#123`; `tracker_url` — an http(s) link; an empty value, `null` in the API, removes the link) can be changed and removed only by the user, rule violations — by the user or the project agent |
| `ask <key> --text T [--option O ...] [--context C] [--recommended O]` | ask the user a question |
| `say <thread> --text T` | post to a task thread or to `_project` |
| `thread <thread>` | read a thread |
| `rebase <KEY>` | move the task branch onto the current base branch together with uncommitted work and wake the agent (user or project agent; a task agent is refused) |
| `ship <KEY>...` | send approved tasks to review with the project's command (user only*) |
| `tracker create\|check\|sync` | create tasks in the tracker (`tracker` stage), check tracker access, or sync statuses now (user only*; on errors — code 1) |
| `add-task --json '{...}'` | add a task (fields: key, title, description, done_when, repo, kind, estimate, deps; `repo` is a name from `repos` if they are set); user only*; task agents write to the project agent (`say _project`), and it proposes the task with `propose` |
| `deps <KEY> --json '[...]'` | replace a task's dependencies with a list of keys (a cycle or an unknown task — code 1; user only*) |
| `propose <add_task\|set_deps\|cancel_task> --json '{...}' [--reason R]` | a project agent proposal |
| `stage <stage>` | move the project to a stage (user only*) |
| `config get [--json]` · `config set --json '{...}' [--dry-run]` · `config validate` | show project settings, change fields (with all save-time checks; `--dry-run` only checks), or validate the saved file (user only*) |
| `config schema [--json]` | allowed settings fields: type, default value, description, enums and the shape of nested objects (tracker, review, ship, harness); built from the settings class and its checks, needs neither a board nor a project |
| `events [--since N]` | event log |
| `lock-run <resource> -- <command...>` | run a command holding an exclusive project resource (queued per resource) |

\* User commands (`stage`, `ship`, `tracker`, `config`, `add-task`, `deps`, `update`) act as the user only when `AGENT_BOARD_ACTOR` is not set; agents are started with it (an empty value also counts as an agent), so they are refused on user-only actions. This protects against mistakes and is not a trust boundary (see "Limitations").

All commands except `serve`, `init`, `stop`, `version`, `status` without arguments and `config schema` talk to the running board; if there is none — exit code 2. Request errors — code 1.

## Execution

`serve` starts the runner: it listens to board events and turns them into agent turns (`claude`, `codex`; `fake` for tests).

- Project stages: `decomposition` → `tracker` (creating tasks in the tracker, can be skipped) → `work` → `done`. Only the user changes the stage (`agent-board stage <stage>`, `POST /api/p/<slug>/stage {"stage", "actor": "user"}`), and stages never go backwards. Task agents work only at the `work` stage: before it, wake-ups accumulate and are kept, and on the move to `work` the tasks with pending wake-ups and the ready tasks start (within limits). The project agent works at any stage; on the move to `work` a project agent without a session starts, as on runner startup. At the `decomposition` stage a user message in a task thread wakes the project agent instead of the task agent (`agent_wake {key: "_project", reason: "message", task: <KEY>}`), so the project agent reads comments on decomposition tasks.
- A task starts when it is ready (no unfinished dependencies). It gets a worktree `<repo>/.agent-board-worktrees/<KEY>` (the directory is added to `.git/info/exclude`) and a branch from `branch_template`; the base comes from the dependencies, along the chain.
- One session runs one turn at a time; wake-ups during a turn are merged into the next message. Concurrency is limited by `max_agents` per project and `global_max_agents` per server; the rest wait in a queue. The project agent counts towards neither limit: it talks to the user, so it runs at once on top of them (one turn per project), and task agents cannot take its slot. A queued turn has `agent_state: waiting_turn` and `wait_reason` (`project` or `global` — which limit is reached); the card, the open card and the project panel show it with a hint on how to raise the limit. Raising `max_agents` or `global_max_agents` starts queued turns right away.
- Claude agents are always allowed the board subcommands `status`, `update`, `ask`, `say`, `show`, `thread`, `state`, `events`, `propose`; `lock-run` and other commands follow `permission_mode`. The system prompt is passed on every turn, including resumed sessions.
- Codex runs with `-c sandbox_workspace_write.network_access=true`: without network access a sandboxed agent cannot reach the local board.
- The raw stream of each turn is written to `<project>/logs/<KEY>.jsonl`.
- **Committing and pushing are not the agents' job.** Neither the prompt nor the board commands ask them to (the trust boundary is described in "Limitations"). An agent sets only `in_progress` and `in_review`; only the user sets `approved`. The board commits to the task's local branch (`<KEY> <title>`) when the user approves the task; dependent tasks start from that commit. If an agent turn is running at that moment, the board stops it first and then commits. An approved task gets no new turns: user messages and answers are recorded in the thread, and the agent receives the accumulated reasons whenever the task leaves `approved` — when it is returned to work, and also when the board itself moves it back to review after a failed commit. Only sending to review pushes. No commit is made if files still contain conflict markers: the task goes back to `in_review`.
- Fields maintained by the runner (`worktree`, `branch`, `base_branch`, `session_id`, `agent_state`, `turn_error`, `last_turn_at`, `harness`, `turn_process`, `agent_parked`, `wait_reason`, `review`, `ship`) can be changed through the API only by the `system` actor. `tracker_key` and `tracker_url` are set by the board when it creates tasks in the tracker, or by the user when the task already exists in the tracker. The board runs git operations only in the worktree `<repo>/.agent-board-worktrees/<KEY>`; if the card has a different path, the operation is skipped with an error on the card.
- When a dependency is approved, the board rebases dependent tasks that have a session, carrying their uncommitted changes along; the agent resolves conflicts by editing files. During the rebase the work is kept in `refs/agent-board/<KEY>/carry` (the shared `git stash` is not used); if it cannot be restored into the files, the worktree files stay as in the rebased branch, all the work is in the ref, and the card shows how to restore it (`git stash apply <ref>`, then `git update-ref -d <ref>`). While the ref exists, the next rebase of this task is not performed. The board does not touch a rebase already in progress in the worktree.
- A failed turn is retried after 5 s, then after 30 s; after 3 failures in a row the task goes to `error` and waits for a message, a status change or a retry from the user. Retry: `agent-board retry <KEY|_project>` (`POST /api/p/<slug>/tasks/<KEY|_project>/retry {"actor": "user"}`), only for an agent in `error`, otherwise 400: the failure counter is reset, the turn continues the same session (a new one if the first turn failed) and gets the previous turn's unhandled reasons together with a note that the turn broke off with the given error; the journal gets `agent_wake {reason: "retry", error}`. If the harness cannot find the session ("No conversation found"), the task starts over in a new session; this does not count as a failure.
- Manual rebase onto the base: `agent-board rebase <KEY>` (`POST /api/p/<slug>/tasks/<KEY>/rebase {"actor"}`): the same rebase for a task that fell behind its base branch, e.g. one without dependencies after other tasks were merged into the base (otherwise review shows other tasks' changes as reverts and conflicts surface late). Called by the user and the project agent (it is not a commit or a push); a task agent gets 403. The journal gets `agent_wake {reason: "rebase", by}`; during a turn the rebase waits for it to end, a blocked task waits to be unblocked. Tasks in `approved`, `sent`, `done`, `cancelled`, without a worktree and session, or outside the `work` stage get 400.
- Agents can wake each other (a task agent writes to `_project`, the project agent writes to a task), but no more than 5 wake-ups of the same agent in a row without the user. The sixth is not delivered: a system message goes to the agent's thread, the task gets the `agent_parked` flag (for the project agent — `project_agent.parked` in the snapshot), and the agent waits for a message, an answer or a status change from the user. The counter lives in server memory and is reset by a restart together with the flags.
- Review, sending to review and tracker sync are described in the "Review", "Sending to review" and "Tracker" sections below.
- `lock-run`: an agent runs long resource-heavy commands (a test stand, a database, an emulator) via `agent-board lock-run <resource> -- <command>`; resources are described in `resources`, and the queue survives a board restart.

**Restart.** A normal `serve` shutdown (Ctrl-C, SIGTERM) terminates the processes of running turns (SIGTERM, SIGKILL after 10 s) but leaves them marked as running on the board. If the server crashed, the agents keep working. In both cases, on the next start the board finds the interrupted turns, stops any remaining process (only if it is recognised as the same one), wakes the agent with reason `interrupted` in the same session and delivers the accumulated messages. Tasks whose turn was waiting in the queue continue with reason `queued_restart`.

### Project settings (`<home>/projects/<slug>/project.json`)

Settings are changed with `agent-board config set`, the "Project settings" window (the gear next to the project name in the header; a `dry_run` check runs before saving, errors are shown next to the field) or `POST /api/p/<slug>/config` (user only): on save, the changed fields are fully checked — the ship template (placeholders outside quotes, no `$'…'` and `<<`), tracker keys and the kind of `auth`, status targets, links, repositories (an absolute path to an existing git repository, unique names), harness, review tool, limits (integers ≥ 1), resources, `branch_template` (only `{key}`, required). The directory and `git` are checked only for new and changed (by name and path) repositories; slow checks run before the board lock is taken, and under the lock the settings are re-read and the result is checked for shape (types, enums), so a stale value in another field does not block the change. If the same field was changed elsewhere at the same time — up to three attempts, then 409. `config validate` (empty `fields`) checks the whole saved file. An unknown field, `slug`, `stage` (use the `stage` command) and `project_session_id` (maintained by the runner) — 400. A change writes a `config_changed {fields}` event. The deprecated `ship_command` field is moved into `ship.command` (if that is empty) the first time the board reads the file and is removed from the file, with a `config_migrated {field, to, moved}` event. If the file cannot be written, the error goes to the log and the project runs on the migrated settings in memory (the write is retried on the next read).

| Key | Purpose |
|---|---|
| `harness` | `{"type": "claude"\|"codex"\|"fake", "permission_mode": ..., "model": ..., "allowed_tools": [...]}`, default `claude` with `auto`. `allowed_tools` (Claude only) — extra allowed tools, for example `"Bash(python3 -m pytest:*)"`: under `acceptEdits` the agent cannot run tests or builds without them |
| `review` | `{"tool": "crit"\|"none", "binary": "crit"}` — review tool, crit by default |
| `ship` | `{"command": str, "timeout": 600, "on_approve": false}` — the command that sends work to review (a template); without a command sending is unavailable; `on_approve` sends a task right after approval |
| `tracker` | `{"type": "none"}` (default); Jira: `{"type": "jira", "url", "project", "issue_type", "epic": "PROJ-1"\|null, "epic_field": "customfield_10007"\|null, "components": [], "labels": [], "extra_fields": {}, "link_type": "Blocks", "title_template": "{title}", "auth": {"type": "token_env", "env": "JIRA_TOKEN", "user_env": "JIRA_USER"} \| {"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"}, "statuses": {"<Jira status>": "<board status>"}, "jira_search_path": "/rest/api/2/search"}` (an epic without `epic_field` is set via `parent`; the default status search path is for Jira Server/DC, Jira Cloud may need `/rest/api/2/search/jql`); GitHub: `{"type": "github", "repo": "owner/name", "labels": [], "auth": {"type": "token_env", "env": "GITHUB_TOKEN"}, "statuses": {"open": null, "closed": "done"}, "api_url": "https://api.github.com"}`. By default statuses are not mapped |
| `repos` | `[{"name", "path", "base"}]` — project repositories |
| `branch_template` | task branch template, default `{key}` |
| `max_agents` | concurrent turns per project (3) |
| `global_max_agents` | concurrent task agent turns per server (6); project agents run on top of it |
| `resources` | `[{"name", "description"}]` — resources for `lock-run` |
| `links` | project links for the UI: `[{"title", "href"}]`, `href` is http(s) or a path to a local file: absolute or relative (to the first repository in `repos`, or, without repositories, to the project directory `<home>/projects/<slug>`). A file is served via `GET /api/p/<slug>/file?path=` only from inside the project directory, its repositories and the task worktrees; html is served with `Content-Security-Policy: sandbox`, files up to 2 MB |
| `language` | language agents use with the user: summaries, questions, thread messages, reports; the board's own system messages in threads and the log are written in it too (already saved messages are not rewritten when it changes) (default `ru`) |
| `agent_rules` | extra project rules: they go into the starting prompt of tasks and of the project agent, not into the system prompt |
| `project_brief` | project description for the project agent |
| `decomposition_rules` | decomposition rules (text up to 4000 characters): they go into the project agent's prompt, and it checks the task graph against them |

Codex support has not been tested against a live `codex`.

Besides the tasks, the project snapshot (`state`) contains `project_agent: {state, turn_error, session_id, parked}` (the runner marks every change of the project agent's state or parking with a `project_agent_changed {state, parked}` event), `project_unread` and `project_thread_size` (unread items and the number of messages in the `_project` thread), and every task has `locks: [{resource, position}]` (0 — the task holds the resource, above 0 — its place in the queue).

## Review

`review: {"tool": "crit"|"none", "binary": "crit"}`. The user opens a round with the "Start review" button (`POST /api/p/<slug>/tasks/<KEY>/review {"actor": "user"}`, only for an `in_review` task); the runner runs `crit --no-open --base-branch <base> -o <project>/crit` in the task worktree and puts the link into `task.review.url`.

- A round with no unresolved comments approves the task on behalf of the user (message "Approved in review (round N)", commit as with the approve button). With comments, the agent is woken with reason `review` and their texts, replies via `crit comment --reply-to`, and once its turn is over and the task is back in review, the runner opens the next round. Finishing a round without new comments approves the task only if there are no unresolved comments at all: while comments from earlier rounds are unresolved, there is no approval, and the board wakes the agent again with the same comments. The reviewer resolves comments in the crit UI, after which an empty "Finish Review" approves the task (or press "Approve" on the board).
- A round is closed when the task is approved, cancelled, or returned to work by the user. On a board restart an open round reconnects to the same crit session (`--session`, same port, same link); the review page is not closed when the board stops.
- `"tool": "none"` — no rounds are started, changes are viewed in the "Changes" tab. Approve without crit — `POST .../tasks/<KEY>/approve {"actor": "user"}`, return with a comment — `POST .../tasks/<KEY>/return {"actor": "user", "text"}`.
- `task.review`: `{round, state: idle|open|changes|fixing|approved, url, session, process, error, history: [{round, state, comments, at}]}`.
- crit output goes to `<project>/logs/<KEY>.review.log`; when a round opens, only the last 1 MB of the log is kept. The task's review data (`<project>/crit/reviews/<session>` and `<project>/crit/sessions/<session>.json|.log`) is deleted by the runner 7 days after the task moves to `done`/`cancelled` (checked at server start and once a day).

## Sending to review

`ship: {"command": "...", "timeout": 600, "on_approve": false}` — the project's command that sends the task branch (push, merge request). By default the board never pushes on its own: only on the button or `agent-board ship <KEY>...` (`POST /api/p/<slug>/ship {"keys": [...], "actor": "user"}`), only for `approved` tasks, without `ship.command` — 400.

- `on_approve: true` — the user's approval itself sends the task: after the approval commit (the button or a review round without comments) the task is queued exactly as by `ship <KEY>` (same queue, logs, `sent` on exit code 0, a thread message on error). If the approval commit fails, nothing is sent. A dependency that is still being sent goes first, and the task is sent after it (skipped if the dependency fails); a dependency that is approved but not sent is an error in the thread — send it, then the task. Research tasks are not sent. `on_approve` without `ship.command` is rejected when the settings are saved.

- The request queue goes one task at a time in dependency order: leftovers are committed in the worktree (`<KEY> <title>`), then `bash -lc <command>` runs in the task worktree (its own process group, the server environment plus `AGENT_BOARD_TASK`; on `timeout` the group is killed, and so are background processes left behind after it finishes).
- Placeholders `{key} {title} {branch} {base} {worktree} {repo} {tracker_key} {tracker_url}` are substituted with `shlex.quote`; unknown ones — 400. Do not quote them (`"{title}"` is rejected: the values are already escaped); `$'…'` and inline `<<` (heredoc) are not supported. `{base}` is the branch of the single unfinished dependency, otherwise the repository's base branch; several unfinished dependencies are an error ("send them first"). `{tracker_key}` and `{tracker_url}` are the task's tracker issue (an empty string without a link); the command also gets them in `AGENT_BOARD_TRACKER_KEY` and `AGENT_BOARD_TRACKER_URL`.
- Example "push + move the issue in the tracker" (with `on_approve: true` approval alone does both): `git push -u origin {branch} && /abs/path/tracker-to-review.sh {tracker_key}` — the script moves the issue to In Review in your tracker and exits with 0 when there is no key.
- Exit code 0 moves the task to `sent` (as `system`); an error or timeout keeps the status and posts a message to the thread, dependent tasks of the request are skipped (`ship.state = skipped`), independent ones go on. A command that was running when the server stopped is marked `interrupted` after the start and is not restarted: send the task again. Cancelling a task stops its sending; a task in `sent` does not wake the agent until it is returned to work.
- Full output — `<project>/logs/<KEY>.ship.log` (each send starts the log over), the card shows the last 4 KB. The project's tracker secrets (token, cookie) are masked as `***` in the log before it is written to disk, in the tail on the card and in the thread message; do not print other secrets and do not put them into the command: it is stored in `project.json`.
- `task.ship`: `{state: queued|running|done|error|skipped|interrupted|cancelled, at, output_tail, exit_code, error, reason}`; events `ship_requested`, `ship_started`, `ship_finished {ok, exit_code}`.

## Tracker

Jira (Server/DC and Cloud, REST v2) and GitHub Issues are supported. Settings are the `tracker` key in `project.json` (the full list of keys is in the table above), default `{"type": "none"}`.

Jira Server with a token from an environment variable (`user_env` switches to Basic instead of Bearer):

```json
"tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
            "epic": "PROJ-1", "epic_field": "customfield_10007", "link_type": "Blocks",
            "auth": {"type": "token_env", "env": "JIRA_TOKEN"},
            "statuses": {"Open": "not_started", "In Progress": "in_progress", "In Review": "sent", "Done": "done"}}
```

Jira with a cookie session from a file (Jira only):

```json
"tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
            "auth": {"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"}, "statuses": {}}
```

GitHub:

```json
"tracker": {"type": "github", "repo": "owner/name", "auth": {"type": "token_env", "env": "GITHUB_TOKEN"},
            "statuses": {"open": null, "closed": "done"}}
```

**Secrets** come only from a server environment variable (`auth: {"type": "token_env", "env": "…"}`) or a file (`auth: {"type": "cookie_file", "path": "…"}`); they cannot be stored in `project.json` (extra `auth` keys are rejected). Secrets never appear in responses, the snapshot, events, logs or error texts (values and their JSON/URL forms are masked as `***`), request headers and bodies are not logged, and tracker redirects are not followed.

**Creating tasks** happens at the `tracker` stage, only on the button (after a confirmation showing the number of tasks and the tracker address): `POST /api/p/<slug>/tracker/create {"actor": "user"}` or `agent-board tracker create`. Tasks without `tracker_key` (except cancelled ones) are created in dependency order; tasks the user has already linked to the tracker (`update <KEY> --tracker-key …`) are skipped, and links to them are created as usual. Creation: the title is `title_template` (`{key}`, `{title}`), the description is the task description plus "Done when: …" and "Depends on: …". The key and the link are written to `tracker_key`/`tracker_url` right away. Then links are created: in Jira — an `issueLink` of type `link_type` ("blocker blocks dependent"; the direction was checked only against the documentation — verify once on your Jira that the dependent issue says "is blocked by"); GitHub has no links — the dependency is written in the issue text. A partial failure does not roll back what was created, a retry continues from what is missing; on an auth, configuration or network error creation stops immediately. There are no internal request retries: if the tracker created an issue but the response was lost, a retry creates a duplicate. Response: `{created, linked, errors, warnings, events}`, event `tracker_created`.

**Access check** — `POST .../tracker/check {"actor": "user"}` → `{ok: true}` or `{ok: false, error, kind}` (`kind`: `config`, `auth`, `not_found`, `validation`, `retryable`, `network`, `error`). If the tracker address (Jira `url`, GitHub `api_url`) is `http://` on a non-local host, the response has a `warning`: secrets are sent unencrypted; the same warning appears in `tracker_sync.warning` and in the sync indicator.

**Status sync.** At the `work` stage and for one more day after moving to `done`, a background server thread (a worker is started only for projects with sync enabled whose time has come) reads the statuses of created issues every `tracker.poll_seconds` (default Jira 30 s, GitHub 120 s — there it is one request per issue) and moves board tasks as the `tracker` actor according to `tracker.statuses` (status names are case-insensitive; `null` — a known status with no transition).

- Allowed targets are `not_started`, `in_progress`, `sent`, `done`; others are rejected when settings are saved (only the user approves; "In Review" in Jira means `sent`).
- By default (`statuses` empty) statuses are not mapped: sync only reads and shows "unknown statuses". The board never writes statuses to the tracker.
- First-read rule: a transition happens only when the tracker status changed since the previous read (the "key, status" pair is remembered), the mapped status differs from the board status, and the task did not change during the read. The first read is only remembered, unless the mapped status is `done`. This way a board that is ahead of the tracker is not rolled back. Other rules are the usual ones: `done` unblocks dependents, a return to `in_progress` wakes the agent.
- The result of a pass is `tracker_sync {at, ok, error, paused, unknown_statuses, changed, errors, warning}` in the snapshot; an unavailable tracker issue goes to `errors`, the rest are synced. The `tracker_synced` event is written only when something changed.
- Pause and retry: network errors and 5xx are retried with a doubling delay (from the interval up to 5 minutes). Auth and configuration errors set `paused: true` (Jira with an expired cookie answers 200 as to an anonymous user: if no issue was found in a pass or the response is marked `X-AUSERNAME: anonymous`, the board asks `/rest/api/2/myself`, and anonymous or 401 is an auth error) — no requests are made until the tracker settings change or the user presses "Retry" (`POST .../tracker/sync {"actor": "user"}` / `agent-board tracker sync`: one pass now, the pause is cleared on success).
- Projects are synced in parallel, at most 4 at a time; a project stuck on a tracker request does not hold up the others and is skipped until its pass finishes.
- The thread is enabled in `serve`; in code it is `BoardServer(tracker_sync=True)` (disabled by default).

| API | What it does |
|---|---|
| `POST /api/p/<slug>/tasks/<KEY>/review` | open a review round |
| `POST /api/p/<slug>/tasks/<KEY>/approve`, `.../return` | approve / return with a comment |
| `POST /api/p/<slug>/tasks/<KEY>/rebase` | move the task onto the current base (`actor`: `user` or `project_agent`) |
| `POST /api/p/<slug>/ship {"keys"}` | send approved tasks with the project's command |
| `POST /api/p/<slug>/tracker/create`, `/check`, `/sync` | create tasks, check access, sync now |
| `GET /api/p/<slug>/config?actor=user` | project settings `{config}` (no secrets in them: only environment variable names and paths) |
| `POST /api/p/<slug>/config {"actor", "fields", "dry_run"}` | change settings fields under the board lock → `{config, saved, events}`; `dry_run: true` only checks and returns the result, `fields: {}` with `dry_run` checks the saved file |
| `GET /api/p/<slug>/config/schema` | the fields the user sets (as `agent-board config schema --json`): `{fields: [{name, type, default, description, shape?}], not_settable}` |

Every request carries `{"actor": "user"}` and the `X-Board-Token` header.

## Environment variables

| Variable | Purpose |
|---|---|
| `AGENT_BOARD_HOME` | data directory (default `~/.agent-board`) |
| `AGENT_BOARD_PROJECT` | default project instead of `--project` |
| `AGENT_BOARD_ACTOR` | who acts: `agent`, `user`, `project_agent`; the runner sets it for agents itself. Not set: for `say` and user commands — `user`, for `status <KEY> <STATUS>` — `agent` (that command is mostly called by agents, while a message from the terminal is written by a human) |
| `AGENT_BOARD_TASK` | the agent's task; with it, `say _project` wakes the project agent (escalation) |
| `AGENT_BOARD_LANG` | language of CLI messages and board errors: `en` (default) or `ru`; the runner sets it for agents from the project language. The CLI passes the language to the board in the `X-Board-Lang` header |

## Interface

The board opens via the link printed by `serve` (`http://127.0.0.1:8737/?token=...`). The token from the link is saved in the `board_token` cookie for 30 days (HttpOnly, SameSite=Strict): during that time the address without the token is enough, after that you need the link from `serve` again. The token does not change across restarts, so an old link keeps working. The server listens only on `127.0.0.1`.

The interface is in Russian; the labels below are given in translation.

What is where:

- **Board** (`#/p/<slug>`) — columns by status, filters (repository, kind, search), the project stage bar, progress, connection indicator. Next to the stage bar is the project's next step (with a confirmation): at the `tracker` stage the "Start work" button (`work`), and at the `work` stage, when all tasks are done or cancelled, "Finish project" (`done`). The "Planning" row in the header holds the project links (`links`) and the decomposition page. Resources are shown on task cards (holds the resource, or waits in the queue with its position) and in the task card. The status is changed by dragging a card or through the status menu; "Blocked" is not a drag target: instead there is an "External blocker…" item with a reason.
- **Graph** — the same board as a dependency graph, toggled with "Board / Graph" in the header; the critical path is highlighted.
- **Task card** (`#/p/<slug>?task=<KEY>`) — a panel on the right with the tabs "Overview", "Thread", "Review", "Changes" ("Review" shows the crit rounds): agent questions and answers to them, conversation, diff. The tabs change only the panel content.
- **Project** — a header button (with the number of open proposals): a panel with the project agent state, the "Project" thread (conversation with the project agent) and its proposals (apply or reject).
- **Decomposition** (`#/p/<slug>/decomposition`) — decomposition tasks with rule checks; at the `decomposition` stage the project agent reads comments on tasks.
- **"Waiting for you"** — a strip above the board: open questions, project agent proposals, tasks in review, agent errors, agents stopped by the conversation limit, external blockers, decomposition rule violations. A click opens the relevant card or page.

Keyboard: `Esc` closes a menu, dialog or card (one per press; does not fire during IME input); `Cmd/Ctrl+Enter` sends a message or an answer; `Enter` or `Space` on a card opens it; the status menu opens with the context-menu key or `Shift+F10` on a card, or with the "⋯" button; after the menu closes, focus returns to the card.

Live updates come over SSE. If the server was restarted or the connection dropped, the indicator shows "Reconnecting…", the tab reconnects by itself (stream with `?since=<last event number>`), re-reads the snapshot and returns to "Online" without reloading the page. A failed snapshot read is retried automatically (after 1, 2, 4, 8 s), a hung snapshot request is aborted after 15 s. If the stream does not open and the server answers "no access" (the cookie expired), the indicator shows "No access" and reconnecting stops: open the link from `serve` again. Re-rendering does not reset the open card and tab, the panel width, scrolling, the draft or focus.

Security: text from agents and the user (summaries, threads, questions, descriptions) is never executed. It is rendered as text; supported markup is paragraphs, `code`, code blocks and `http(s)` links with `rel="noopener noreferrer"`; HTML, `javascript:` and `data:` stay text. There are no external requests (system fonts), and the board works offline.

## Tests

Every release bumps the version in `pyproject.toml`, `board/agent_board/__init__.py`, `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (tests check that they match): by the version the board notices that an old server is still running after a plugin update and asks to restart it.

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The interface is ES modules in `board/agent_board/static/`, with no build step and no dependencies; the server serves them at `/static/...`. The UI logic is tested with Node's built-in test runner:

```bash
node --test 'board/tests/js/*.test.mjs'
```

Headless Chrome checks (puppeteer is installed only in `ui-tests/`; Node is needed only for development and does not affect running the board) start a real server on a temporary directory with a fake agent and save screenshots to `ui-tests/out/`. All scripts in a row (stops with a non-zero code on the first failure):

```bash
cd ui-tests && npm install && npm test
```

The Python interpreter for the board is picked in `ui-tests/python.mjs`, first match wins: the `AGENT_BOARD_PYTHON` environment variable, `.venv` in the repository root, `.venv` of the main checkout (so the checks run from a task worktree without its own `.venv`), `python3` from `PATH`. If none is found, the scripts stop with an error naming these options.

Scripts one by one (`node ui-tests/<name>.mjs` from the root or `npm run <name>` in `ui-tests/`):

- `smoke` — end-to-end pre-release scenario: columns and "Waiting for you", answering an agent and sending a message, tabs without recreating the panel, agent text is not executed (summary and thread), draft/focus/scroll under the event stream, server restart with reconnection without reloading, 60 tasks and 200 events in 5 s with no long tasks > 200 ms, a forbidden transition with an error toast (the load thresholds can be changed: `SMOKE_LONGTASK_MS`, default 200, and `SMOKE_SEND_LIMIT_MS`, default 7000 — the time limit for sending 200 events);
- `skeleton` — skeleton, live updates, safe markdown;
- `board` — 14 tasks in all statuses, status menu, toasts; mockup snapshots alongside for comparison;
- `card` — task card with the runner and the fake agent: answering a question, thread, diff, resizing; tab and mockup snapshots;
- `graph` — graph, the "Project" panel, a new task;
- `decomposition` — the decomposition page.

The task card opens at `#/p/<slug>?task=<KEY>`; the panel width is stored in `localStorage`, message and answer drafts in `sessionStorage`. The board view (columns or graph) and whether the "Project" panel is collapsed are also stored in `localStorage`.

## Publishing

The script exports the current tree (tracked files and new non-ignored files, without `.superpowers/`) into a new repository as a single commit. The commit author comes only from the arguments, the machine's git settings are not used; the script pushes nothing:

```bash
.venv/bin/python -m pytest -q board/tests/test_publishability.py   # no internal names or personal paths
python3 scripts/export-repo.py ../agent-board-public \
    --author-name "Quantulion" --author-email "<id>+Quantulion@users.noreply.github.com"
```

Use GitHub's noreply address for the commit email (Settings → Emails → "Keep my email addresses private"): it does not reveal your real address. The repository URL in `.claude-plugin/plugin.json` and `marketplace.json` (`homepage`, `repository`) must match the repository created on GitHub.

## License

MIT, full text in [LICENSE](LICENSE).
