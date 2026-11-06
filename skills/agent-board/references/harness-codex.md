# Codex

- Skill dir = the directory this skill was read from (`${CODEX_HOME:-~/.codex}/skills/agent-board`, or
  `.agents/skills/agent-board` in a repo). CLI: `sh <skill-dir>/scripts/agent-board`.
- The `workspace-write` sandbox may block writes to the board home (`~/.agent-board`) and network to localhost.
  If you see permission denied on the home, or exit 2 / connection refused while `status` shows the board running,
  run board commands with escalated (unsandboxed) execution; tell the user why and ask them to approve it.
  Alternative: the user sets `AGENT_BOARD_HOME` to a directory inside the workspace.
- `serve --detach` should run outside the sandbox, otherwise the server may die with the command.
- Env vars may not persist between commands: set them inline.
- Codex agents launched by the board are configured in the project (`harness: {"type": "codex", "sandbox": …}`);
  the board enables network access for them itself.
