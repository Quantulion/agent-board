# Web interface

Open the link printed by `agent-board serve` (or `agent-board status`). The interface is in Russian or English,
following the project's `language` setting (`ru` — Russian, anything else — English), and has light, dark and
automatic themes. It is plain ES modules served by the board, with no build step and no external requests.

## Screens

- **Projects** (`#/`) — the list of projects.
- **Board** (`#/p/<slug>`) — columns by status, filters (repository, kind, search), the project stage bar with the
  next step ("Start work" at the `tracker` stage, "Finish project" when all tasks are done), progress and the
  connection indicator. The "Planning" row holds the project links and the decomposition page. Change a status by
  dragging a card or from its status menu; "Blocked" is not a drag target, use "External blocker…" with a reason
  instead. "New task" adds a task with its dependencies.
- **Graph** — the same tasks as a dependency graph ("Board / Graph" in the header), with the critical path
  highlighted.
- **Task card** (`#/p/<slug>?task=<KEY>`) — a panel on the right with the tabs "Overview", "Thread", "Review" and
  "Changes": agent questions and answers, conversation, review rounds, the diff against the base branch. The card
  shows the agent state (working, waiting for a free slot, error with "Retry", parked), resources, the tracker link
  and shipping state.
- **Project** — a header button with the number of open proposals: the project agent state, the "Project" thread
  and the proposals. Each proposal shows what will change (new task and links, settings with old and new values,
  the status transition, the tasks to ship, the stage) and the agent's reason; apply or reject it with one button.
- **Breakdown** (decomposition, `#/p/<slug>/decomposition`) — tasks with rule checks and the "Approve breakdown" button; at the `decomposition` stage comments on
  tasks go to the project agent.
- **Project settings** — the gear next to the project name, see [Configuration](configuration.md#changing-settings).
- **"Waiting for you"** — a strip above the board: open questions, proposals, tasks in review, agent errors, parked
  agents, external blockers, decomposition rule violations. A click opens the relevant card or page.

## Threads

While an agent works, the bottom of its thread shows a "working" indicator with the turn time, and an "Interrupt
current work" checkbox: a message sent with it stops the turn and is answered at once (see
[Interrupting a turn](workflow.md#interrupting-a-turn)). A failed turn shows its error.

## Header marks

- "Review off" — `auto_approve` is on.
- Tracker sync indicator — the result of the last pass, with "Retry" when sync is paused.

## Keyboard

`Esc` closes a menu, dialog or card (one per press); `Cmd/Ctrl+Enter` sends a message or an answer; `Enter` or
`Space` on a card opens it; the status menu opens with the context-menu key, `Shift+F10` or the "⋯" button.

## Live updates

Updates arrive over SSE. If the server restarts or the connection drops, the indicator shows "Reconnecting…", the
tab reconnects by itself (`?since=<last event>`), re-reads the snapshot and returns to "Online" without reloading.
A failed snapshot read is retried after 1, 2, 4 and 8 s; a hung request is aborted after 15 s. If the server answers
"no access" (the cookie expired), reconnecting stops: open the link from `serve` again. Re-rendering keeps the open
card and tab, panel width, scroll, drafts and focus.

The panel width, the board view (columns or graph), the collapsed "Project" panel and the theme are stored in
`localStorage`; message drafts in `sessionStorage`.
