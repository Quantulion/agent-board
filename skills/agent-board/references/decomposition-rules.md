# Decomposition rules

Check every task against the project's rules and record what breaks them. The page shows violations to the
user, who decides. `<slug>` is the project slug.

## Steps

1. Read the rules: `agent-board --project <slug> config get --json` → `decomposition_rules`.
   Empty → say so in the summary, offer to set them (`references/setup.md`, question 8), record nothing.
2. Split the text into separate rules. Check each task (`agent-board --project <slug> state`) against each rule:
   title, description, `done_when`, `repo`, `kind`, `estimate`, `deps`.
3. For every task with violations, write the full list (it replaces the previous one):
```
agent-board --project <slug> update PAY-4 --rule-violations-json '[{"rule": "One deploy per task", "text": "Changes api and worker: two deploys. Fix: split into PAY-4 (api) and PAY-5 (worker), PAY-5 depends on PAY-4."}]'
```
   - `rule`: the rule as the project states it, short.
   - `text`: what exactly is wrong in this task + the concrete fix you suggest.
4. A task that now complies but had violations recorded earlier → clear them: `--rule-violations-json '[]'`.
5. Do not fix anything yourself. Splitting, merging, re-scoping is the user's call: list your proposed fixes
   in the summary and apply one only after the user says yes (then re-check that task). Text, estimate and
   dependencies you may then change (`references/intake.md`, step 5). Cancelling or splitting an existing task
   needs a status change: that is the user's action on the board; never change statuses yourself.

## Typical rules and what a violation looks like

| Rule | Violation | Suggested fix |
|---|---|---|
| One deploy per task | Task changes two services or needs two releases | Split per service, chain by deps |
| Task ≤ 3 days | `estimate` 5 | Split by layer or by step of the migration |
| Every task has a checkable `done_when` | Empty, or "works", "done" | Name the test, command or observable result |
| Research before a risky change | Data migration with no research task before it | Add a `research` task, make the change depend on it |
| Tests in the same task | "Write tests for X" as a separate task | Merge tests into the task that changes X |
| One repo per task | Description touches two repos | Split per repo |

## Common mistakes

| Mistake | Fix |
|---|---|
| Rewriting the task so the violation disappears | Record it; the user decides |
| `text` says only "violates the rule" | Say what is wrong and how to fix it |
| Appending one violation and losing the others | Send the whole list for the task |
