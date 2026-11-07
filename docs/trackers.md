# Trackers

The board can create its tasks as issues in Jira (Server/DC and Cloud, REST v2) or GitHub Issues and then follow
their statuses. The tracker is off by default (`"tracker": {"type": "none"}`). The board never writes statuses to
the tracker; to move issues when work is shipped, use the [ship command](review-and-ship.md#shipping).

## Settings

Jira keys:

| Key | Purpose |
|---|---|
| `url`, `project`, `issue_type` | required |
| `auth` | required: `{"type": "token_env", "env": "JIRA_TOKEN", "user_env": "JIRA_USER"}` (`user_env` switches Bearer to Basic) or `{"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"}` |
| `epic`, `epic_field` | an epic key (`PROJ-1`) and the custom field that holds it (`customfield_10007`); without `epic_field` the epic is set via `parent` |
| `components`, `labels`, `extra_fields` | extra fields of created issues |
| `link_type` | the link type for dependencies (default `Blocks`) |
| `title_template` | issue title, placeholders `{key}` and `{title}` (default `{title}`) |
| `statuses` | `{"<Jira status>": "<board status>" \| null}` |
| `poll_seconds` | status sync interval (default 30) |
| `jira_search_path` | default `/rest/api/2/search` (Server/DC); Jira Cloud may need `/rest/api/2/search/jql` |

GitHub keys: `repo` (`owner/name`, required), `auth` (`token_env` only, required), `labels`, `title_template`,
`statuses` (`{"open": …, "closed": …}`), `api_url` (default `https://api.github.com`), `poll_seconds` (default 120:
GitHub is read with one request per issue).

Examples:

```json
"tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
            "epic": "PROJ-1", "epic_field": "customfield_10007", "link_type": "Blocks",
            "auth": {"type": "token_env", "env": "JIRA_TOKEN"},
            "statuses": {"Open": "not_started", "In Progress": "in_progress", "In Review": "sent", "Done": "done"}}
```

```json
"tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PROJ", "issue_type": "Task",
            "auth": {"type": "cookie_file", "path": "~/.config/agent-board/jira-cookie.txt"}, "statuses": {}}
```

```json
"tracker": {"type": "github", "repo": "owner/name", "auth": {"type": "token_env", "env": "GITHUB_TOKEN"},
            "statuses": {"open": null, "closed": "done"}}
```

**Secrets** come only from a server environment variable (`token_env`) or a file (`cookie_file`); they cannot be
stored in `project.json` (extra `auth` keys are rejected). Secrets never appear in responses, the snapshot, events,
logs or error texts (values and their JSON/URL forms are masked as `***`), request headers and bodies are not
logged, and tracker redirects are not followed. If the tracker address is `http://` on a non-local host, the board
warns that secrets go unencrypted.

## Creating issues

At the `tracker` stage, on the button (after a confirmation with the number of tasks and the tracker address),
`agent-board tracker create` or `POST /api/p/<slug>/tracker/create`.

- Tasks without `tracker_key` (except cancelled ones) are created in dependency order. Tasks the user already
  linked to an existing issue (`agent-board update <KEY> --tracker-key …`) are skipped, and links to them are
  created as usual.
- The title comes from `title_template`; the description is the task description plus "Done when" and "Depends on".
  The key and the URL are written to the task at once.
- Then dependency links are created: in Jira an issue link of type `link_type` ("blocker blocks dependent"; the
  direction was checked only against the documentation, so verify once that the dependent issue says "is blocked
  by"); GitHub has no links, so the dependency is written in the issue text.
- A partial failure does not roll back what was created, and a retry continues from what is missing; on an auth,
  configuration or network error creation stops. There are no internal retries: if the tracker created an issue but
  the response was lost, a retry creates a duplicate.
- Response: `{created, linked, errors, warnings, events}`; event `tracker_created`.

**Access check**: `agent-board tracker check` → `{ok: true}` or `{ok: false, error, kind}` (`config`, `auth`,
`not_found`, `validation`, `retryable`, `network`, `error`).

## Status sync

At the `work` stage, and for one more day after `done`, a background thread reads the statuses of created issues
every `poll_seconds` and moves board tasks as the `tracker` actor according to `statuses` (status names are
case-insensitive; `null` means a known status with no transition).

- Allowed targets: `not_started`, `in_progress`, `sent`, `done`; others are rejected when settings are saved. Only
  the user approves, so "In Review" in Jira means `sent`.
- With empty `statuses` (default) sync only reads and shows "unknown statuses".
- A task moves only when the tracker status changed since the previous read, the mapped status differs from the
  board's, and the task did not change during the read. The first read is only remembered, unless it maps to
  `done`. So a board that is ahead of the tracker is not rolled back. `done` unblocks dependents, a return to
  `in_progress` wakes the agent.
- The result of a pass is `tracker_sync {at, ok, error, paused, unknown_statuses, changed, errors, warning}` in the
  snapshot; an unavailable issue goes to `errors`. The `tracker_synced` event is written only when something
  changed.
- Network errors and 5xx are retried with a doubling delay (up to 5 minutes). Auth and configuration errors pause
  sync (`paused: true`) until the tracker settings change or the user presses "Retry" (`agent-board tracker sync`:
  one pass now). Jira with an expired cookie answers as to an anonymous user; the board detects that and treats it
  as an auth error.
- Projects are synced in parallel, at most 4 at a time; a stuck project does not hold up the others.
- `serve --no-tracker-sync` turns the thread off.
