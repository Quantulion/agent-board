# Project setup

Configure a board project by asking the user short questions and writing the answers with the board CLI.
`agent-board` below is this skill's `scripts/agent-board`; `<slug>` is the project slug.

## Ground rules

- Source of truth for fields, types, defaults and allowed values: `agent-board config schema --json`. Read it first.
- Ask ONE question per message, in the user's language, with a recommended default the user can accept by "ok".
- The board must be running: `agent-board serve --detach`.
- Read the current settings: `agent-board --project <slug> config get --json`. A field that differs from its schema
  default is already configured: skip it unless the user asks to change it.
- Never write `project.json` or any board file yourself. Every change goes through `config set`.
- `config set` replaces each top-level field whole: send the complete object (current value + the change).

## Writing a field

1. Validate: `agent-board --project <slug> config set --json '{"max_agents": 3}' --dry-run`
2. Show the user the normalized value from the reply (`config.<field>`), not your input.
3. On confirmation apply the same command without `--dry-run`.
4. Error (exit 1): explain the message in plain words, fix with the user, repeat from step 1.

Batch several confirmed answers into one `config set` when they are ready together.
Put the JSON in single quotes; write an apostrophe inside it as `'\''`.

## Secrets

Never ask for a token, password or cookie value. Auth is configured only as an env var NAME or a file PATH.
If the user pastes a secret value anyway:
- Do not use it in any command, file or setting, and do not repeat it back.
- Say it was not saved and ask for an env var name or a file path instead.
- Recommend rotating the secret: it is now in the chat history.

## Questions, in order

### 1. Repositories → `repos`
Ask for each repo's path (default: the current git repo). Check it:
`git -C <path> rev-parse --show-toplevel`. Use that absolute path; `name` = directory name (unique).
Base branch default: `git -C <path> symbolic-ref --short refs/remotes/origin/HEAD` without `origin/`,
else the current branch. Ask "more repos?" until no.
```
agent-board --project <slug> config set --json '{"repos": [{"name": "api", "path": "/abs/path/api", "base": "main"}]}' --dry-run
```
Branch name template (`branch_template`): default `{key}`; ask only if the user has a naming convention.
It must contain `{key}` and nothing else in braces.

### 2. Agent CLI and permissions → `harness`
Default `type`: the CLI you are running in (`claude` or `codex`). Explain plainly: task agents run in the background,
nobody is there to approve a command, so the mode decides what they may do unattended.
- Claude: recommend `permission_mode` `auto`. `acceptEdits` is stricter but agents stop on commands it does not allow;
  `bypassPermissions` allows everything — only on a disposable machine.
- Codex: recommend `sandbox` `workspace-write`; the board always enables network so agents reach it.
```
agent-board --project <slug> config set --json '{"harness": {"type": "claude", "permission_mode": "auto"}}' --dry-run
```

### 3. Parallel agents → `max_agents`
Default 3. Ask: "How many agents may work at once in this project?"

### 4. Review → `review`
Run `crit --version`. Exit 0: propose `{"tool": "crit", "binary": "crit"}`. Otherwise propose `{"tool": "none"}`
(changes are reviewed in the board's Changes tab) and mention crit can be installed later.

### 5. Tracker → `tracker`
Ask: none, Jira or GitHub Issues? Default none. Linear is not supported.
- Jira: `url` (https), `project` key, `issue_type` (default `Task`), `epic` key or null. Epic on Jira Server/DC:
  ask for the Epic Link field id → `epic_field` (e.g. `customfield_10007`); Jira Cloud: leave `epic_field` out.
  Optional: `components`, `labels` (lists of strings).
- GitHub: `repo` as `owner/name`, optional `labels`.
- Auth, Jira: env var with a token (`{"type": "token_env", "env": "JIRA_TOKEN"}`, add `user_env` for Basic login)
  or a cookie file (`{"type": "cookie_file", "path": "~/keys/jira.txt"}`). GitHub: `token_env` only.
  The env var must be set in the environment the board server was started from; if it was not, restart:
  `agent-board stop`, export the variable, `agent-board serve --detach`.
```
agent-board --project <slug> config set --json '{"tracker": {"type": "jira", "url": "https://jira.example.com", "project": "PRJ", "issue_type": "Task", "epic": "PRJ-1", "epic_field": "customfield_10007", "components": [], "labels": [], "auth": {"type": "token_env", "env": "JIRA_TOKEN"}}}' --dry-run
```
After saving run `agent-board --project <slug> tracker check`. On failure show the error and fix the setting;
do not ask for the secret.

Status mapping (`statuses`): tracker status name → board status. Allowed targets: `not_started`, `in_progress`,
`sent`, `done`, or null (known status, no move). Never map anything to `approved`: only the user approves.
Propose this default and ask the user to rename keys to their workflow's real status names:
- Jira: `{"Open": "not_started", "To Do": "not_started", "In Progress": "in_progress", "In Review": "sent", "Done": "done", "Closed": "done"}`
- GitHub: `{"open": null, "closed": "done"}`

Save the confirmed mapping inside the full `tracker` object.

### 6. Ship command → `ship`
The command that pushes a task branch and opens a PR/MR. It runs in the task worktree only when the user presses
"Ship". Placeholders: `{key} {title} {branch} {base} {worktree} {repo}`. Values are shell-escaped already:
never put a placeholder in quotes (`"{title}"` is rejected); `$'…'` and heredocs `<<` are rejected too.
Examples to offer:
- GitHub: `git push -u origin {branch} && gh pr create --base {base} --head {branch} --title {title} --fill`
- Own script: `/abs/path/ship.sh {key} {branch} {base}`

No secrets in the command: it is stored in the settings. Default: none (shipping disabled, can be set later).
```
agent-board --project <slug> config set --json '{"ship": {"command": "git push -u origin {branch} && gh pr create --base {base} --head {branch} --title {title} --fill", "timeout": 600}}' --dry-run
```

### 7. Shared resources → `resources`
Ask: "Is there anything only one agent may use at a time — functional tests, a local stand, a shared database?"
For each: `name` (latin letters, digits, `-`, `_`, `.`) and `description` (what it is, how to run it).
Agents wrap such commands in `agent-board lock-run <name> -- <command>`.
```
agent-board --project <slug> config set --json '{"resources": [{"name": "functional-tests", "description": "Run with make ftest"}]}' --dry-run
```

### 8. Rules
- Agent rules → `agent_rules` (text in every agent prompt). Look for `CLAUDE.md` and `AGENTS.md` in each repo and
  offer to reference them by absolute path. Ask for build and test commands and skills agents must load.
- Decomposition rules (task size, one repo per task, tests in the same task, …) → a `## Decomposition rules`
  section of `project_brief`. Keep that section whenever `project_brief` is rewritten later.

### 9. Language → `language`
Default: the language the user writes in (`ru`, `en`, …). It sets the board UI and agent messages.

## Finish

Run `agent-board --project <slug> config validate` and show a short summary of what was set and what was skipped.

## Common mistakes

| Mistake | Fix |
|---|---|
| Several questions in one message | One question, one default |
| Saving without `--dry-run` first | Dry run, show normalized result, then save |
| Sending only the changed key of `tracker` | Send the whole object |
| `"{title}"` in the ship command | Bare `{title}` |
| Token or cookie value in a setting or command | Env var name or file path only |
| Mapping a status to `approved` | Only `not_started`, `in_progress`, `sent`, `done`, null |
