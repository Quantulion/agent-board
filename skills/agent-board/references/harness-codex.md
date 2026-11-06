# Codex

- Skill dir = the directory this skill was read from (`${CODEX_HOME:-~/.codex}/skills/agent-board`, or
  `.agents/skills/agent-board` in a repo). CLI: `sh <skill-dir>/scripts/agent-board`.
- Sandbox: `workspace-write` blocks writes to the board home (`~/.agent-board`) and network to localhost.
  Symptoms: permission denied on the home, or exit 2 / connection refused while `status` shows the board running.
  Run board commands with escalated (unsandboxed) execution; ask the user to approve it once and say why.
- `serve --detach` must run outside the sandbox, or the server dies with the command.
- Env vars do not persist between commands: set them inline.
- Codex agents launched by the board are configured in the project (`harness: {"type": "codex", "sandbox": …}`);
  the board enables network access for them itself.
