# Review, approval and shipping

The path of a code task after its agent is done: `in_review` → review rounds → `approved` (the board commits) →
`sent` (the project's ship command ran) → `done` (merged into the base branch).

## Review

`review: {"tool": "crit" | "none", "binary": "crit"}`. [crit](https://github.com/tomasz-tomczyk/crit) is an
inline review tool in the browser; it is optional.

The user opens a round with the "Start review" button on an `in_review` task (`POST .../tasks/<KEY>/review`). The
runner runs `crit --no-open --base-branch <base> -o <project>/crit` in the task worktree and puts the link into
`task.review.url`.

- A round finished with no unresolved comments approves the task on behalf of the user ("Approved in review
  (round N)", commit as with the Approve button).
- With comments, the agent is woken with the comment texts, replies with `crit comment --reply-to`, and once its
  turn is over and the task is back in review, the runner opens the next round by itself.
- While comments from earlier rounds are unresolved there is no approval; the board wakes the agent again with the
  same comments. The reviewer resolves comments in crit, after which an empty "Finish Review" approves the task (or
  press "Approve" on the board).
- A round closes when the task is approved, cancelled or returned to work. After a board restart an open round
  reconnects to the same crit session (same port, same link).
- crit output goes to `<project>/logs/<KEY>.review.log` (the last 1 MB is kept). Review data of a task is deleted 7
  days after the task moves to `done`/`cancelled` (checked at server start and once a day).

`"tool": "none"`: no rounds; changes are viewed in the card's "Changes" tab, and the user approves with "Approve"
or returns the task with a comment ("Return", `POST .../tasks/<KEY>/return {"actor": "user", "text"}`).

`task.review`: `{round, state: idle|open|changes|fixing|approved, url, session, process, error, history: [{round,
state, comments, at}]}`.

## Approval

Only the user approves: the "Approve" button, a clean review round, `POST .../tasks/<KEY>/approve`, or an applied
`set_status` proposal. On approval the board stops a running agent turn, commits the work to the task branch
(see [Committing](workflow.md#committing)), moves dependent tasks onto it, and with `ship.on_approve` queues the
task for shipping.

### Auto-approve

`auto_approve: true` is a fast mode without code review. When a task agent moves a `code` task to `in_review`, the
board approves it at once as `system` by the same rules as a user approval: the turn is stopped, the work is
committed, dependents are rebased, and with `ship.on_approve` the task is sent. No review round is opened. The thread
gets "Approved automatically (auto_approve)", the journal gets `status_changed` with `actor: "system"` and
`auto: true`. If the commit fails (conflict markers and so on), the task goes back to `in_review`. Research tasks
are never auto-approved. While the mode is on, the project header shows a "Review off" mark.

## Shipping

`ship: {"command": "...", "timeout": 600, "on_approve": false, "on_failure": "stay"}` — the project's command that
sends the task branch on (push, pull/merge request, moving the tracker issue). Without a command shipping is off,
and an approved task shows a hint to configure it or merge the branch by hand.

By default the board never pushes on its own: only on the "Ship for review" button (one task or several),
`agent-board ship <KEY>...` or `POST /api/p/<slug>/ship {"keys": [...]}`, and only for `approved` tasks.

### Automatic shipping

`on_approve: true` makes the approval itself send the task: after the approval commit the task is queued exactly as
by `ship <KEY>`. If the commit fails, nothing is sent. A dependency that is still being sent goes first and the
task follows it (skipped if the dependency fails); a dependency that is approved but not sent is an error in the
thread. Research tasks are not sent. `on_approve` without `ship.command` is rejected when settings are saved.

### When the command fails

`on_failure` decides what a failed command does to the task:

- `stay` (default): the task stays `approved`, the output goes to the thread, and the user sorts it out.
- `return`: a non-zero exit code (not a timeout or cancel) moves the task to `in_progress` and wakes its agent with
  reason `ship_failed` and the last 3000 characters of the output. Before the turn the board moves the task branch
  onto the fresh base branch, as `agent-board rebase` does: the task's commits become uncommitted changes, with
  conflict markers where they clash. The agent fixes the cause, runs the tests and returns the task to review; after
  the next approval the task is sent again.

### The command

- The queue runs one task at a time in dependency order. Leftover changes are committed in the worktree first, then
  `bash -lc <command>` runs in the task worktree in its own process group, with the server environment plus
  `AGENT_BOARD_TASK`, `AGENT_BOARD_TRACKER_KEY` and `AGENT_BOARD_TRACKER_URL`. On `timeout` (seconds) the group is
  killed, as are background processes left behind.
- Placeholders: `{key} {title} {branch} {base} {worktree} {repo} {tracker_key} {tracker_url}`. They are substituted
  already shell-escaped, so do not quote them (`"{title}"` is rejected). Unknown placeholders, `$'…'` and inline
  `<<` heredocs are rejected.
- `{base}` is the branch of the single unfinished dependency, otherwise the repository's base branch; several
  unfinished dependencies are an error ("send them first"). `{tracker_key}`/`{tracker_url}` are empty without a
  tracker link.
- Exit code 0 moves the task to `sent`. An error or timeout keeps the status (unless `on_failure: return`) and posts
  the output to the thread; dependent tasks of the same request are skipped, independent ones go on. A command that
  was running when the server stopped is marked `interrupted` and is not restarted. Cancelling a task stops its
  shipping.
- Full output: `<project>/logs/<KEY>.ship.log` (each send starts the log over); the card shows the last 4 KB. The
  project's tracker secrets are masked as `***` in the log, the card and the thread. Do not put other secrets into
  the command: it is stored in `project.json`.
- `task.ship`: `{state: queued|running|done|error|skipped|interrupted|cancelled, at, output_tail, exit_code, error,
  reason}`; events `ship_requested`, `ship_started`, `ship_finished {ok, exit_code}`.

Example — push and move the issue in the tracker (with `on_approve: true` approval alone does both):

```json
"ship": {"command": "git push -u origin {branch} && /abs/path/tracker-to-review.sh {tracker_key}", "on_approve": true}
```

The script moves the issue to "In Review" in your tracker and exits with 0 when there is no key.

## Merged into the base branch

At server start and every 5 minutes the runner checks tasks in `approved` and `sent` (except research tasks and
tasks being shipped right now): are they merged into their repository's base branch (`base` in `repos`, `main` by
default)? The local branch is checked; the board does not run `git fetch` or `pull`. A task is merged when its
approval commit and the tip of its branch (if it still exists) are in the base history. A merged task moves to
`done` with the thread message "Merged into <base>", and dependent tasks are moved onto the base.

A branch with no commits of its own (nothing to commit on approval) is never taken as merged: the user closes such a
task. Cards of tasks in `approved`/`sent` show a "not merged into <base>" mark.
