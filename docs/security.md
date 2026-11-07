# Security and trust boundary

agent-board is a local, single-user tool. Read this before giving agents broad permissions.

## Network and access

- The server listens only on `127.0.0.1` and checks the `Host` header (and `Origin` on POST requests).
- Every API request needs the access token: the `X-Board-Token` header or the `board_token` cookie. The token lives
  in `<home>/token` and does not change across restarts.
- The link printed by `serve` (`http://127.0.0.1:8737/?token=…`) sets the cookie for 30 days (HttpOnly,
  SameSite=Strict); after that, open the link again.
- That link ends up in chat history. The risk is small (the board is local and the token file is readable by the
  same OS user anyway), but do not publish those logs. To rotate the token: stop the board, delete `<home>/token`,
  start it again.

## Users and agents

Actions are split between actors: `user`, `agent` (task agent), `project_agent`, and the board itself (`system`,
`tracker`).

- Agents set only `in_progress` and `in_review`; they cannot approve, ship, change the stage, the settings, the
  dependencies, the task wording or their own priority.
- Committing and pushing are not the agents' job. The board commits on approval and pushes only through the ship
  command, which the user starts (or allows with `ship.on_approve`).
- The project agent acts for the user only through [proposals](workflow.md#proposals) the user applies, or with
  `project_agent_status` for the listed statuses.

**This separation protects against mistakes, not against an agent deliberately acting as the user.** The actor is
taken from the `AGENT_BOARD_ACTOR` variable and the `actor` field of a request, and agents run as the same OS user
and can read `<home>/token`. An agent with unrestricted shell access (`permission_mode: auto` or
`bypassPermissions`) can run a command without `AGENT_BOARD_ACTOR` or call the API directly and perform user
actions: approving, shipping, changing the stage or settings, committing and pushing with git. If this matters to
you, use `acceptEdits` with a narrow `harness.allowed_tools`, or the Codex sandbox.

## Git

- The board runs git operations only in the task's own worktree `<repo>/.agent-board-worktrees/<KEY>`; if a card
  points elsewhere, the operation is skipped with an error on the card.
- Fields maintained by the runner (worktree, branch, session, agent state, review, ship and so on) can be changed
  through the API only by `system`.
- The board does not run `git fetch`, `pull` or `push` by itself; pushing happens only in the ship command.

## Secrets

Tracker secrets come only from environment variables or files and are masked everywhere, see
[Trackers](trackers.md#settings). The ship command is stored in `project.json`: do not put secrets into it.

## The web UI

Text from agents and users (summaries, threads, questions, descriptions) is never executed. It is rendered as text;
supported markup is paragraphs, `code`, code blocks and `http(s)` links (`rel="noopener noreferrer"`); HTML,
`javascript:` and `data:` stay text. The page is served with a strict Content Security Policy, makes no external
requests and works offline. Local files from project links are served only from inside the project's directories,
HTML in a sandbox.
