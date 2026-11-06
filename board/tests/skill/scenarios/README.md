# Scenario kit for the agent-board skill

The controller runs agents through the skill and checks the board afterwards. Nothing here touches `~/.agent-board`.

Files: `plan-6-tasks.md`, `ready-list.md`, `resume-board.md`, `secret-token.md` (one scenario each),
`fixtures/plan.md`, `make_env.py`, `check_state.py`, `common.py`. `{repo}` in scenario texts is `<dir>/repo`.

## Running one scenario

1. Create the environment (a new empty directory per run):
   `python3 make_env.py /tmp/ab-s1` (scenario 3: add `--existing-project`). Run the printed `export` lines in the
   agent's shell: they set `AGENT_BOARD_HOME=<dir>/home`, `AGENT_BOARD_PYTHON` and `AGENT_BOARD_REPO`.
2. Give the agent this prompt, filling the placeholders from the scenario file (replace `{repo}` with `<dir>/repo`):

   > You have the agent-board skill at `<root>/skills/agent-board/SKILL.md`; the user says: `<first message>`;
   > simulated user answers: `<answers section>`; act as the agent in a terminal session; use the wrapper
   > `<root>/skills/agent-board/scripts/agent-board` (with `AGENT_BOARD_HOME` and `AGENT_BOARD_PYTHON` from the
   > environment); don't ask questions beyond the simulated answers.

   Baseline (scenario 1 only): same prompt without the skill path; the agent is told the CLI exists as the wrapper.
3. Check the state: `python3 check_state.py <dir> <scenario>` (names or numbers 1-4). Exit 0 and `OK` mean pass;
   otherwise each deviation is printed as `FAIL: ...`. Scenarios 1, 2, 4 start the server if it is stopped and
   stop it after the check; scenario 3 requires the server running and stops it after the check
   (`--keep-server` leaves it).
4. Scenario 1 idempotency: run the same prompt again in the same `<dir>` with a second agent, then
   `python3 check_state.py <dir> plan-6-tasks --rerun` (compares count and titles with the snapshot the first
   successful check wrote). Scenario 4: pass the agent's transcript with `--transcript FILE`.
5. Cleanup: `sh <root>/skills/agent-board/scripts/agent-board stop` (with the env exports), then delete `<dir>`.

Use `python3` of 3.9+ and run all commands from this directory or with absolute paths.
