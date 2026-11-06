# Scenario 2: ready task list and «запусти»

Environment: `make_env.py <dir>` (no project yet).

## User's first message

> Вот готовый список задач, закинь на доску и запусти:
> 1. Add parser module for the input format.
> 2. Add validator module (after 1).
> 3. Add reporter module (after 2).
> 4. Update docs with the input format description.
> 5. Add benchmark script (after 1 and 3).
>
> Репозиторий `{repo}`.

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

The agent must not start anything: "запусти" is the user's button after approval. Expect it to explain that
and give the decomposition link.

## Expected end state (`check_state.py <dir> ready-list`)

- Exactly 5 tasks (the user's list is not re-split); deps: validator -> parser, reporter -> validator,
  benchmark -> parser + reporter; docs independent; every task has a non-empty `done_when`.
- Stage `decomposition`, all tasks `not_started`, no `turn_started` events.
