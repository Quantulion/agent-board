# Scenario 3: bring back the board

Environment: `make_env.py <dir> --existing-project` (project `demo` with one task, server stopped).

## User's first message

> Подними доску проекта demo.

## Simulated user answers

No setup questions are expected (the project is already configured). If the agent asks anything, answer "ok".
Repos path placeholder, if needed: `{repo}`.

## Expected end state (`check_state.py <dir> resume-board`)

- Board server is running (`status` shows it).
- The only project is still `demo`; its config and tasks are identical to `baseline.json`.
- The agent printed `<board url>#/p/demo/decomposition` and a short state summary; it did not run `init`,
  `config set`, `add-task` or any stage change.
