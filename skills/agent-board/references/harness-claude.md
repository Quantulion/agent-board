# Claude Code

- Skill dir = the base directory Claude Code prints when the skill loads. CLI: `sh <skill-dir>/scripts/agent-board`.
- Run every board command as a normal Bash call. `serve --detach` returns at once: never use `run_in_background` for it.
- `export` does not persist between Bash calls: put env vars in the same command.
- Permission prompts: the user can allow the CLI once with `Bash(sh <skill-dir>/scripts/agent-board:*)` in their
  settings, with `<skill-dir>` replaced by the absolute base directory shown for this skill (never the placeholder). Offer it when the prompts get in the way; do not edit settings yourself.
- Notifications: watch `<board home>/server.log` (Monitor) only if the user asks to be told about board events.
- Agents the board launches are configured in the project (`harness`, `permission_mode`), not here.
