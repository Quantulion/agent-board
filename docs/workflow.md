# How work flows

## Project stages

`decomposition` → `tracker` (creating tasks in the tracker, can be skipped) → `work` → `done`.

Only the user changes the stage (`agent-board stage <stage>` or the buttons next to the stage bar), and stages never
go backwards.

- **decomposition** — the task graph is prepared and reviewed. Task agents do not run. A user message in a task
  thread wakes the project agent instead of the task agent, so the project agent reads comments on decomposition
  tasks and turns them into proposals.
- **tracker** — tasks are created in the tracker on a button, see [Trackers](trackers.md).
- **work** — task agents run. On the move to `work` the ready tasks and the tasks with wake-ups accumulated
  before start (within limits), and a project agent without a session starts.
- **done** — the project is finished ("Finish project" appears when every task is done or cancelled).

The project agent works at any stage. In a project without a plan (`kind: free`) there are no stages to switch: it
starts at `work`, and the board does not start its project agent without a session on a server start — only a user
message in the "Project" thread does.

## Tasks and statuses

| Status | Meaning |
|---|---|
| `not_started` | waiting for dependencies or for a free slot |
| `blocked` | shown, not set: unfinished dependencies or an external blocker |
| `in_progress` | the agent works on it |
| `waiting_answer` | the agent asked a question |
| `queued` | waiting in the queue |
| `in_review` | the agent finished; the user reviews |
| `approved` | the user approved; the board committed the work |
| `sent` | the ship command succeeded |
| `done` | finished (merged into the base branch, synced from the tracker, or closed by hand) |
| `cancelled` | dropped; its dependents lose the dependency |

A task agent sets only `in_progress` and `in_review` and cannot reopen a `done` or `cancelled` task. Only the user
sets `approved`, `sent` and `done` (the board and the tracker sync also move tasks as `system`/`tracker`; the
project agent may with [`project_agent_status`](configuration.md#project-agent-statuses)).

Task kinds: `code` (own worktree, review and shipping) and `research` (the result is a report in the task's
`report` field; never auto-approved or shipped, the user closes it).

A task starts when it is ready: no unfinished dependencies and no external blocker. With `chains: true` (default) a
dependency counts as finished once it is `approved`, `sent` or `done`, and the dependent task starts from the
dependency's branch, along the chain; with `chains: false` only `done` counts.

Each code task gets a worktree `<repo>/.agent-board-worktrees/<KEY>` (the directory is added to
`.git/info/exclude`) and a branch from `branch_template`.

In a project by plan a task names one of the project's repositories. In a project without a plan a new task may
instead give a path to any git repository on the disk (`repo_path`): a repository the project does not have yet is
added to its settings automatically — named after its folder (`api-2` if `api` is taken), with its default branch
as the base. The same repository given by another path (a symlink, `~`, a trailing `/`) is not added twice. Details:
[API](api.md#repository-by-path).

## Agents and turns

The board runs two kinds of agents through the [harness](configuration.md#harness) CLI:

- a **task agent** per task, in the task's worktree;
- one **project agent** per project, which talks to the user in the "Project" thread, reads comments during
  decomposition and prepares [proposals](#proposals).

An agent works in turns. A wake-up (a new task, an answer, a message, a review comment, a returned task…) starts a
turn; wake-ups that arrive during a turn are merged into the next one. The system prompt is passed on every turn,
including resumed sessions. The raw stream of each turn is written to `<home>/projects/<slug>/logs/<KEY>.jsonl`.

### Concurrency and priorities

Concurrency is limited by `max_agents` per project and `global_max_agents` per server (a
[board setting](configuration.md#board-settings)); the rest wait in a queue.
A queued turn has `agent_state: waiting_turn` and `wait_reason` (`project` or `global` — which limit is reached);
the card, the open card and the project panel show it with a hint on how to raise the limit.

The queue is ordered by task `priority` (`high`, `normal` by default, `low`): when a slot frees up, waiting `high`
tasks start first and `low` ones last; within one priority, in wake-up order. Running turns are not interrupted.
The priority is changed by the user or the project agent (`agent-board update <KEY> --priority high`), not by task
agents; a change writes `priority_changed {key, from, to, actor}` and re-sorts the queue at once.

The project agent counts towards neither limit: it talks to the user, so it runs at once on top of them (one turn
per project).

### Interrupting a turn

A message with interrupt is for when the agent is busy with a long turn (tests, long commands) and the answer
should not wait for its end: the "Interrupt current work" checkbox under the thread (shown while the agent works),
`agent-board say <KEY|_project> --text T --interrupt`, or `"interrupt": true` in the message request. Only the user
and the project agent may send it.

If the agent is in a turn, the board stops it (a signal to the process group, up to 10 s of waiting, then a forced
kill; background processes of the turn are stopped too) and starts a new turn in the same session at once: first
the message, then a note that the previous turn was interrupted, then the reasons of the interrupted turn. This is
not a failure. The journal gets `turn_interrupted {key, message_id, by}`. If the agent is queued or idle, it is a
plain message. During decomposition a comment on a task goes to the project agent without interrupting it.

### Failures and retrying

A failed turn is retried after 5 s, then after 30 s; after 3 failures in a row the agent goes to `error` and waits
for the user. If the harness cannot find the session ("No conversation found"), the task starts over in a new
session; this does not count as a failure.

Retry continues the same session (a new one if the first turn failed) with the previous turn's unhandled reasons
and a note about the error: the "Retry" button next to the error (task card, project panel, "Waiting for you"),
`agent-board retry <KEY|_project>`, or a `retry` proposal of the project agent. Only the user retries; only an
agent in `error`; a task must be at the `work` stage and not `approved`, `sent` or `cancelled`.

### Agents waking each other

A task agent can write to the project agent (`say _project`) and the project agent to a task, but no more than 5
wake-ups of the same agent in a row without the user. The sixth is not delivered: a system message goes to the
agent's thread, the agent is marked as parked, and it waits for a message, an answer or a status change from the
user. The counter lives in server memory and is reset by a restart.

### Restart

A normal `serve` shutdown (Ctrl-C, SIGTERM) terminates running turns (SIGTERM, SIGKILL after 10 s) but leaves them
marked as running. If the server crashed, the agents keep working. In both cases, on the next start the board finds
the interrupted turns, stops any remaining process (only if it is recognised as the same one), wakes the agent with
reason `interrupted` in the same session and delivers the accumulated messages. Turns that were waiting in the
queue continue with reason `queued_restart`.

## Committing

Committing and pushing are not the agents' job; nothing asks them to. The board commits to the task's local branch
(`<KEY> <title>`) when the task is approved; if an agent turn is running at that moment, the board stops it first.
No commit is made while files contain conflict markers: the task goes back to `in_review`. New files in the
top-level `reports/` directory (agents' working reports) are never committed, even if the repository does not
ignore it; changes to files git already tracks are committed.

An approved task gets no new turns: messages and answers are recorded, and the agent receives them when the task
leaves `approved` (returned to work, or moved back to review after a failed commit). Only [shipping](review-and-ship.md#shipping)
pushes.

## Rebasing

When a dependency is approved or done, the board moves dependent tasks that have a session onto it, carrying their
uncommitted changes along; the agent resolves conflicts by editing files.

`agent-board rebase <KEY>` (user or project agent) does the same for a task that fell behind its base branch, for
example a task without dependencies after other tasks were merged into the base. It waits for a running turn to
end; tasks in `approved`, `sent`, `done`, `cancelled`, without a worktree and session, or outside the `work` stage
are refused.

Details:

- The work is kept in `refs/agent-board/<KEY>/carry` during the move (the shared `git stash` is not used). If it
  cannot be restored into the files, the worktree stays as in the rebased branch, and the card shows how to restore
  it (`git stash apply <ref>`, then `git update-ref -d <ref>`). While the ref exists, the task is not rebased again.
- A rebase already in progress in the worktree is not touched.
- If the task branch has its own commits (for example, the approval commit of a task that came back after a failed
  merge), they move too and become uncommitted changes, with conflict markers where they clash with the base; the
  old branch tip is named in the thread. Commits of a dependency already merged into the base are not carried again.

## Proposals

The project agent cannot change the graph or act for the user (except the statuses allowed by
`project_agent_status` and [tasks the user asked for](#tasks-from-the-project-agent)); it prepares a proposal with `agent-board propose <kind> --json '{...}' [--reason R]`, and
the user applies it with one button in the "Project" panel or rejects it (the project agent is woken).

| Kind | Payload | Applied as |
|---|---|---|
| `add_task` | `{"task": {...}}` (fields as in `add-task`) | `add-task` |
| `set_deps` | `{"key", "deps": [...]}` | `deps` |
| `cancel_task` | `{"key"}` | move to `cancelled` |
| `set_config` | `{"fields": {...}}` (as in `config set`; board settings such as `language` are not accepted) | `config set` |
| `set_status` | `{"key", "to", "from"?}` | a status change by the user; `approved` works like the Approve button, only from `in_review` |
| `ship` | `{"keys": [...]}` | `ship` |
| `stage` | `{"stage"}` | `stage` |
| `retry` | `{"key"}` (a task or `_project`) | `retry` |

A proposal is checked when it is created with the same checks as the direct action (`set_config` — a dry run) and
is not saved if they fail. Applying goes as `user` through the same checks. The board remembers the state the
proposal was checked against (task status, stage, current values of the changed settings); if it changed since,
applying is refused as stale, and the proposal stays open — reject it and ask for a new one. Proposals about a
cancelled or missing task, and `add_task` with an existing key, are closed automatically as `obsolete`.

## Tasks from the project agent

With [`project_agent_tasks`](configuration.md#project-agent-tasks) on (default) the project agent creates tasks
directly, without a proposal, but only when the user asked for that work in the "Project" thread:

- the board allows `add-task` from the project agent only in a turn woken by a user message; in any other turn (an
  escalation of a task agent, a board event, a rejected proposal) it is refused with a hint to propose `add_task`;
- if the request splits into several tasks that together cover exactly what was asked (one change in two
  repositories), the agent creates them all, sets dependencies between them and explains in the thread why there are
  several;
- work beyond the request ("while at it, rewrite the tests", a bug noticed nearby) is only a proposal with a button;
- if the agent is not sure how to split the request, it asks in the thread and creates nothing;
- afterwards it lists the created tasks in the thread: key, title, repository, and which repositories were added to
  the project, with their paths.

A created task starts at once if it has no open dependencies, like any new task at the `work` stage. In a project
without a plan the agent finds the repository on disk itself (it reads the file system but does not change it) and
passes its path as `repo_path`. With the setting off the project agent only proposes tasks.

## Shared resources

An agent runs long, resource-heavy commands (a test stand, a database, an emulator) via
`agent-board lock-run <resource> -- <command>`; resources are declared in [`resources`](configuration.md#project-fields).
Only one task holds a resource at a time; the others queue, and the queue survives a board restart. Task cards show
whether the task holds a resource or its place in the queue.
