# HTTP API

The board serves a JSON API on `http://127.0.0.1:<port>/api/`. The CLI and the web UI use it; so can your own
tools. The port and token of the running board are in `<home>/server.json`.

- Every request needs the token: the `X-Board-Token` header (or the `board_token` cookie set by the board link).
- POST and PUT bodies are JSON objects. User actions carry `"actor": "user"`; the actor decides what is allowed (see
  [Security](security.md)).
- `X-Board-Lang: en|ru` selects the language of error messages.
- Errors: `{"error": "…"}` with 400 (invalid request), 401 (no token), 403 (not allowed for this actor or host),
  404 (unknown project, task or route), 409 (settings changed concurrently), 413 (file too large).
- Successful actions answer `{"events": [...]}` with the journal events they produced.

## Read

| Request | Response |
|---|---|
| `GET /api/projects` | `{projects}` — all projects |
| `GET /api/p/<slug>/state` | the project snapshot: `config`, `tasks`, `proposals`, `project_agent {state, turn_error, wait_reason, session_id, parked, turn_process}`, `project_unread`, `project_thread_size`, `tracker_sync`, `last_seq` |
| `GET /api/p/<slug>/events?since=N` | `{events}` after event number `N` |
| `GET /api/p/<slug>/stream?since=N` | Server-Sent Events: the same events live (`Last-Event-ID` is honoured) |
| `GET /api/stream` | Server-Sent Events for all projects: an `event: project` with `{slug, seq, type}` for every journal event of any project, `{slug, seq: 0, type: "project_created"}` when a project is created and `{slug: null, seq: 0, type: "settings_changed", fields}` when the board settings change; no replay, read the details from the project itself |
| `GET /api/settings` | `{settings: {language, global_max_agents, harness, review}}` — the board settings, see [Board settings](#board-settings) |
| `GET /api/settings/schema` | `{fields: [{name, type, default, description, inherited, shape?}]}` |
| `GET /api/p/<slug>/threads/<thread>` | `{messages}` of a task thread or `_project` |
| `GET /api/p/<slug>/tasks/<KEY>/diff` | `{base, files, diff, truncated}` — task changes against its base branch (up to 400 KB) |
| `GET /api/p/<slug>/file?path=P` | a local file from a project link, see [Links](configuration.md#links) |
| `GET /api/p/<slug>/config?actor=user` | `{config}` — the effective settings (no secrets: only variable names and paths): inherited `harness` and `review` filled with the board values, `overrides` — the inherited fields the project sets itself, `language` and `global_max_agents` — the board values; the same `config` is in `/state` |
| `GET /api/p/<slug>/config/schema` | `{fields: [{name, type, default, description, shape?}], not_settable, board_fields, inherited}` |
| `GET /api/fs/complete?path=P` | `{items: [{path, abs, git, branch}]}` — up to 20 folders on this machine matching the typed path, see [Repository by path](#repository-by-path) |

## Tasks

| Request | Body | Who |
|---|---|---|
| `POST /api/p/<slug>/tasks` | `{task, actor}` → also `{key}`; without `task.key` (or with `""`) the board picks the next key: the most common prefix of the project's tasks with the next number, `<SLUG>-1` in an empty project; in a project without a plan `task.repo_path` may replace `task.repo` (see [Repository by path](#repository-by-path)), and the answer also carries `{repo}` | user |
| `POST .../tasks/<KEY>/transition` | `{to, actor}` | by status rules, see [Workflow](workflow.md#tasks-and-statuses) |
| `POST .../tasks/<KEY>/update` | `{fields, actor}` | per field, see [`update`](cli.md#tasks-and-threads) |
| `POST .../tasks/<KEY>/deps` | `{deps, actor}` | user |
| `POST .../tasks/<KEY>/questions` | `{question: {text, options, context, recommended}}` → also `{question_id}` | agent |
| `POST .../tasks/<KEY>/questions/<id>/answer` | `{choice, comment}` | user |
| `POST .../tasks/<KEY>/review` | `{actor}` — open a review round | user |
| `POST .../tasks/<KEY>/approve` | `{actor}` | user |
| `POST .../tasks/<KEY>/return` | `{actor, text}` — back to work with a comment | user |
| `POST .../tasks/<KEY|_project>/retry` | `{actor}` | user |
| `POST .../tasks/<KEY>/rebase` | `{actor}` | user, project agent |

## Threads

| Request | Body |
|---|---|
| `POST .../threads/<thread>/messages` | `{author: user\|agent\|project_agent, text, interrupt?, from_task?}` |
| `POST .../threads/<thread>/read` | mark the thread as read |

## Project

| Request | Body | Who |
|---|---|---|
| `POST /api/projects` | `{actor, title, key, kind: "free", harness_type?, max_agents?, auto_approve?, project_agent_tasks?}` → `{slug, key, title}`, see [New project](#new-project) | user |
| `PUT /api/settings` | `{actor, fields, dry_run}` → `{settings, saved, changed}`, see [Board settings](#board-settings) | user |
| `POST /api/p/<slug>/stage` | `{stage, actor}` | user |
| `POST /api/p/<slug>/config` | `{actor, fields, dry_run}` → `{config, saved, events}`; `fields: {}` with `dry_run: true` checks the saved file; `harness: null` or `review: null` returns the board value; `language` and `global_max_agents` — 400 | user |
| `POST /api/p/<slug>/ship` | `{keys, actor}` | user |
| `POST /api/p/<slug>/tracker/create`, `/check`, `/sync` | `{actor}` | user |
| `POST /api/p/<slug>/proposals` | `{kind, payload, reason}` → also `{proposal_id}` | project agent |
| `POST /api/p/<slug>/proposals/<id>/apply`, `/reject` | — | user |
| `POST /api/p/<slug>/locks/<resource>/acquire`, `/poll`, `/release` | `{task, pid}` → `{position}` | `lock-run` |

### New project

`POST /api/projects` creates a project without a plan (`kind: "free"`); a project by plan is still created with the
skill or `agent-board init`, and `kind: "plan"` is rejected with 400. `key` is the task key prefix: a Latin letter,
then up to 9 letters or digits. The project gets `slug = key` in lower case, and its tasks are `KEY-1`, `KEY-2`…
If the key is taken by another project's slug or by the task key prefix of any project (`AB` while another project
has `AB-1`), the board adds a number: `FIX` → `FIX2`, `FIX3`…; the answer carries the key actually used. The new
project starts at the `work` stage with no repositories and inherits `harness` and `review` from the board settings.
`harness_type` is `claude` or `codex`: equal to the board's agent type it is inherited, otherwise it becomes the
project's own `harness`. The other options are the settings of the same name; `language` is a board setting and is
rejected here.
Nothing is started: agents run only after user actions.

### Board settings

`PUT /api/settings` changes the [board settings](configuration.md#board-settings) — the language, the server-wide
agent limit and the default `harness` and `review` of projects — for the user only (another actor — 403). `fields`
holds the fields to change, each replaced whole and checked as in `config set` (an unknown field or a bad value —
400); `dry_run: true` only checks. The answer: `settings` — the resulting values, `saved`, `changed` — the fields
whose value changed. After a save every project where a changed field is in effect gets a
`config_changed {fields, settings: true}` event, and `/api/stream` gets a `settings_changed` item.

### Repository by path

In a project without a plan (`kind: "free"`) a new task may point to any git repository on the disk with
`"repo_path": "<path>"` instead of `"repo": "<name>"` — in `POST .../tasks`, `agent-board add-task --json` and an
`add_task` proposal. The path may start with `~`, may be a symlink, end with `/` or point inside the repository: the
board uses the real path of the repository root. If the project already has a repository there, the task gets its
name; otherwise the repository is added to `repos` (a `config_changed` event) with the folder name as `name` (`api`,
then `api-2`, `api-3`… when the name is taken), the real path as `path` and the default branch as `base` (the one
`origin/HEAD` points to, otherwise the current branch). A missing folder, a relative path, a folder outside git,
`repo` together with `repo_path`, or `repo_path` in a project by plan — 400.

`GET /api/fs/complete?path=P` suggests folders for such a path: everything up to the last `/` is the parent folder,
the rest is the beginning of the name (case-insensitive). `P` must start with `/` or `~`; hidden folders are listed
only when the name starts with `.`. Each item: `path` as typed (with `~`), `abs` — the absolute path, `git` — the
folder is a repository root, `branch` — its default branch (`null` outside git). A missing folder gives an empty list.

## Events

Every change is an event in `<project>/events.jsonl` with a growing number `seq`, a `type` and `data`:

| Type | Data |
|---|---|
| `task_added` | `key` |
| `status_changed` | `key, from, to, actor`; `auto` — approved by `auto_approve` |
| `task_updated` | `key, fields` |
| `deps_changed` | `key, deps` |
| `priority_changed` | `key, from, to, actor` |
| `task_unblocked` | `key` |
| `agent_wake` | `key` (a task or `_project`), `reason`, extra fields by reason |
| `agent_stop` | `key` |
| `question_asked`, `question_answered` | `key, question_id` |
| `message_posted` | `thread, message_id, author` |
| `thread_read` | `thread` |
| `proposal_created`, `proposal_applied`, `proposal_rejected` | `id` |
| `proposal_obsolete` | `id, reason` (`exists`, `cancelled`, `missing`) |
| `turn_started`, `turn_finished` | `key` |
| `turn_failed` | `key, error` |
| `turn_interrupted` | `key, message_id, by` |
| `task_committed` | `key` |
| `lock_waiting`, `lock_acquired`, `lock_released` | `key, resource` |
| `stage_changed` | `from, to` |
| `config_changed` | `fields` |
| `config_migrated` | `field, to, moved` |
| `project_agent_changed` | `state, parked, wait_reason` |
| `review_requested` | `key` |
| `ship_requested` | `keys` (in shipping order) |
| `ship_started` | `key` |
| `ship_finished` | `key, ok, exit_code` |
| `tracker_created` | `created, linked, errors` |
| `tracker_synced` | `ok, changed, error` (written only when something changed) |

Wake-up reasons (`agent_wake.reason`): `ready`, `unblocked`, `returned`, `answer`, `message`, `proposal_rejected`,
`escalation`, `manual`, `rebase`, `interrupted`, `retry`, `queued_restart`, `review`, `ship_failed`.
