# Scenario 1: plan with 6 tasks

Environment: `make_env.py <dir>` (no project yet). Plan fixture: `{repo}/docs/plan.md` (committed in the repo).
Run it twice for the idempotency check (see README).

## User's first message

> Разложи план на доску: `{repo}/docs/plan.md`. Агентов пока не запускай.

## Simulated user answers to setup questions

Answer each question only when the agent asks it; one short answer per question. Any question not listed: "skip".

- Repositories: `{repo}` (base branch `main`). No more repos.
- Agent CLI: harness `fake` (type `fake`, nothing else).
- Parallel agents: 3.
- Review: none.
- Tracker: none.
- Ship command: none.
- Shared resources: none.
- Decomposition rules: "Every task is at most 2 days. Every task has a checkable done_when. One repo per task."
- Agent rules: skip.
- Language: ru.
- Project slug and title (when asked to confirm): accept the agent's proposal.
- Confirmation of a task table or a `config set` dry run: "ok".

When the agent asks about decomposition rules the answer above applies. Task 2 of the plan is estimated at 3 days:
the agent must record it as a violation of «Every task is at most 2 days», not split it.

## Expected end state (`check_state.py <dir> plan-6-tasks`)

- 6 tasks; chain: commands -> storage, e2e -> commands; logging, readme, research independent.
- Every task has a non-empty `done_when`; the research task has kind `research`.
- The 3-day task (CLI commands) has a rule violation; no other task has one.
- `project_brief` is not empty; `links` contains the path of `docs/plan.md`.
- Stage `decomposition`, all tasks `not_started`, no `turn_started` events.
- Final message contains `<board url>#/p/<slug>/decomposition`, a summary and the violation.
- Re-run (same message, same env, second agent): `check_state.py <dir> plan-6-tasks --rerun` finds the same
  6 tasks and the same titles, no duplicates.
