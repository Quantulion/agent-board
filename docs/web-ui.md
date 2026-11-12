# Web interface

Open the link printed by `agent-board serve` (or `agent-board status`). The interface is in Russian or English,
following the project's `language` setting (`ru` — Russian, anything else — English), and has light, dark and
automatic themes. It is plain ES modules served by the board, with no build step and no external requests.

## Screens

- **Projects** (`#/`) — the home screen, a board of projects. The header shows "N active · M agents working of
  the limit · K waiting for a slot" (the limit is the largest `global_max_agents` among projects), the connection
  indicator and "New project" (see below). A common
  "Waiting for you" strip collects items from all projects, each marked with the project key; a click opens the card
  or the project board. Projects move between columns by themselves:
  - "Waiting for you" — something in the project waits for you (see "Waiting for you" below);
  - "Agents working" — nothing waits for you, but at least one agent (a task's or the project agent) works or waits
    for a slot;
  - "Quiet" — neither;
  - "Archive" — collapsed by default: projects archived from the card's "⋯" menu ("Archive" / "Restore from
    archive", the `archived` setting), and planned projects at the `done` stage. An archived project opens and works
    as usual. If something in it waits for you, the project stays in the archive, its items appear in the common
    strip and the collapsed "Archive" column shows their number.

  A project card shows the key (the prefix of its task keys), the type ("No plan" or "Planned" with the stage),
  working agents, task counts, a progress bar and "X of Y done" for planned projects, chips for questions, review,
  errors and proposals, and up to two repositories with "+N". A click opens the project board. Any column collapses
  and expands; the state is stored in `localStorage` (`ab-home`).

  **New project** dialog (the header button or the dashed "＋ New project" in "Quiet"): title; task key — filled
  from the title (first letters of the words, Cyrillic transliterated, `PRJ` if nothing fits), editable, Latin
  letters and digits starting with a letter, up to 10 characters. If the key is already taken by another project or
  by the task-key prefix of another project, the hint says "FIX is taken, the project will get FIX2" — the server
  numbers the key itself (`FIX2`, `FIX3`…). Type: "No plan" only; "Planned" is shown disabled (for now it is
  created with the skill in the agent CLI). Agents: Claude Code or Codex. "More" (collapsed): language (the current
  interface language by default), agents in the project (3), review before commit (on; off means `auto_approve`),
  project agent creates tasks on request (`project_agent_tasks`, on). Repositories are not asked for. "Create and
  open" sends `POST /api/projects` and opens the new project's board at the `work` stage; no agent starts. Server
  errors are shown under their field.
- **Board** (`#/p/<slug>`) — columns by status, filters (repository, kind, search), the project stage bar with the
  next step ("Start work" at the `tracker` stage, "Finish project" when all tasks are done), progress and the
  connection indicator. The "Planning" row holds the project links and the decomposition page. Change a status by
  dragging a card or from its status menu; "Blocked" is not a drag target, use "External blocker…" with a reason
  instead. "New task" adds a task with its dependencies. Any column folds into a narrow strip with its task count
  with the "Collapse" button in its header; a click on the strip unfolds it. By default "Cancelled" is collapsed, and
  in a project without a plan (`kind: free`) also "Sent" and "Done".
- **Board of a project without a plan** (`kind: free`) — the same board without the plan parts. The header shows
  "← Projects", the title with a "No plan" chip, the gear and "N repositories · limit K agents · review before
  commit" ("review off" with `auto_approve`); there is no stage bar, plan progress bar, "Planning" row, "Board /
  Graph" switch or wave filter. Status counters and "Waiting for you" are as usual. "Not started" and "Blocked" are
  hidden while empty. The "Project" panel is open by default with the hint "Describe what to do — the agent will
  create tasks…"; closing it is remembered for this project only. A task the project agent created at your request is
  signed "Created by the project agent at your request" on its card and in the task card, and a new one is briefly
  highlighted. "New task" here asks for the title, "What to do", the repository and the kind; the key is assigned
  by the board, and "Done when", dependencies, priority and estimate are under "More". The "Repository" field
  suggests the project repositories first, then folders on the board's machine for the typed path (`/…` or `~/…`):
  a git folder shows "git · <default branch>", a plain folder shows "not git" and cannot be picked. A project
  repository is sent as `repo`, a folder as `repo_path`: a new folder is added to the project repositories (see
  [Repository by path](api.md#repository-by-path)). The button is "Create and start" — a task without blockers starts
  as soon as an agent slot is free. In a project with a plan the dialog is unchanged.
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
  agents, external blockers, decomposition rule violations, an unread reply of the project agent in the "Project"
  thread (it goes away once you open the thread). A click opens the relevant card or page.

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

The home screen listens to the common stream `/api/stream` and re-reads the snapshot of the project that changed
(several events in a row are merged), so cards move between columns without reloading.

The panel width, the board view (columns or graph), the filters and collapsed columns (per project), the collapsed
"Project" panel and the theme are stored in `localStorage`; message drafts in `sessionStorage`.
