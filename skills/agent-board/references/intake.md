# Intake: from plan to tasks

Turn the user's input into board tasks with dependencies. `<slug>` is the project slug.

## Inputs

| Input | What to do |
|---|---|
| Plan or spec file (writing-plans, brainstorming, grilling output) | Read it whole. Decompose into tasks |
| Free text in the chat | Same as a plan; ask about gaps before creating anything |
| Ready list or table of tasks | Do not re-split. Keep the user's tasks, fill missing fields and dependencies. No `done_when` → propose one from the text and list it as an open question; no estimate → open question only, not a violation |
| Tracker keys (`PRJ-123`) | Treat as ready tasks; read their text only from what the user gives you. Link each one (below) |

An existing tracker issue is linked after `add-task`, as the user:
`agent-board --project <slug> update PAY-1 --tracker-key PRJ-123` (add `--tracker-url <issue url>` only if the user
gave it). `tracker create` creates issues only for tasks without a key, so a linked task never gets a duplicate.
Never leave a known key only in the description: that task would get a second issue.

## Task fields

`add-task` accepts exactly these keys; anything else is rejected.

| Field | Rule |
|---|---|
| `key` | `<PREFIX>-<n>`: PREFIX is the slug's first word upper-cased, or a short abbreviation of it, 2–6 letters (`items-cli` → `ITEMS`, `payments-pg` → `PAY`), or the prefix existing tasks use; the user may override. Unique, never starts with `_` |
| `title` | What the task delivers, ≤ 80 characters, imperative ("Add PG schema for payments") |
| `description` | What to do: scope, files or components, decisions from the plan that apply. Enough for an agent who has not read the plan |
| `done_when` | A checkable criterion: tests pass, endpoint returns X, report answers Y. Not "done" or "implemented" |
| `repo` | One `name` from `config get --json` → `repos`. A task touching two repos is two tasks |
| `kind` | `code`, or `research` when the result is a report, not a change. Research tasks run in a worktree too: with one repo it is used by default, with several set `repo` at intake |
| `estimate` | Days, a number, only if the plan gives one. Otherwise omit |
| `deps` | Keys of tasks whose code or result this task needs. Order of work alone is not a dependency |

One task = one reviewable, deployable unit. Follow the project's `decomposition_rules` while splitting
(`references/decomposition-rules.md`).

## Steps

0. Read `agent-board --project <slug> config get --json` → `repos`. Empty, or the plan needs a repo that is not
   there → go back to `references/setup.md`, question 1, before drafting. Every task's `repo` must be one of these
   names: the board rejects any other.
1. Read existing tasks: `agent-board --project <slug> state`. A plan task matches an existing one by key,
   or by the same title (ignore case and punctuation).
2. Draft the list and the graph in memory. Never invent work the plan does not contain. Ambiguity that changes
   the split (unclear scope, which repo) → ask the user, one question at a time. The rest → "open questions"
   in the final summary.
3. Show the user a compact table (key, title, repo, deps, estimate) and get a yes before writing.
   More than 15 tasks: group the table by repo or plan section.
4. `add-task` prints an `agent_wake reason=ready` event: before approval it is only queued, no agent runs.
   Create new tasks in topological order (dependencies first), one call each. Many tasks: chain the calls in one
   shell command with `&&`, still in topological order, so the first error stops the rest:
```
agent-board --project <slug> add-task --json '{"key": "PAY-2", "title": "Add PG schema for payments", "description": "Tables payments, refunds; migrations only.", "done_when": "Migrations apply to an empty database in tests", "repo": "api", "kind": "code", "estimate": 2, "deps": ["PAY-1"]}'
```
5. Existing task that matches but whose text or dependencies changed in the plan (stage `decomposition` only).
   Change only what differs:
```
agent-board --project <slug> update PAY-2 --description 'Tables payments, refunds' --done-when 'Migrations apply in tests' --estimate 2
agent-board --project <slug> deps PAY-3 --json '["PAY-1", "PAY-2"]'
```
   `--title` works the same way. `deps` replaces the whole list. `repo` or `kind` changed → tell the user;
   cancelling the old task is their action on the board. Never change a task's status yourself; after they
   cancel it, add the new one with a new key.
6. Errors:
   - "Зависимость от неизвестной задачи" → you broke topological order; create the dependency first.
   - "Зависимости образуют цикл" → the plan has a cycle. Find which edge is only order, not a real need, drop it;
     if unsure, ask the user. Never create a task without its real dependency to get past the error.
   - "Задача уже существует" → it is a match from step 1: skip it.
   - "Репозиторий … не из настроек проекта" → wrong `repo`: use a configured name or add the repo (setup, question 1).
   - Field or JSON error → fix the payload, retry. Put JSON in single quotes; write `'` as `'\''`.
7. Run `agent-board --project <slug> state` again and check: every plan task is there once, deps match the plan.

## Summary for the user

Created (keys), linked to existing tracker issues, updated, skipped as duplicates, open questions (also saved in `project_brief`, SKILL.md step 6). No agent has started: work starts when the user
approves the decomposition on the page.
