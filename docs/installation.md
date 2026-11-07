# Installation

Requirements: Python 3.9+ (standard library only) and git. [crit](https://github.com/tomasz-tomczyk/crit) is
optional, for code review. Agents run through the `claude` (Claude Code) or `codex` CLI, which must be installed and
logged in.

## The skill (plugin)

The `skills/agent-board` skill lays a plan out on the board and starts the board.

**Claude Code** — a plugin from a local marketplace:

```
/plugin marketplace add ~/path/to/agent-board   # absolute path or ~
/plugin install agent-board@agent-board-local
```

**Codex** — a symlink in `${CODEX_HOME:-~/.codex}/skills`, no copies:

```bash
cd /path/to/agent-board
sh scripts/install-codex.sh              # --force replaces a foreign symlink and moves a directory aside to .bak.<timestamp>
sh scripts/install-codex.sh --uninstall  # remove the symlink
```

Harness specifics for the skill: `skills/agent-board/references/harness-claude.md` and `harness-codex.md`.

The skill calls the board through its wrapper `skills/agent-board/scripts/agent-board`. The wrapper prefers the
installed board (below) when it exists; `AGENT_BOARD_FROM_REPO=1` makes it run the code from the repository.

## The board

When the board runs straight from a repository checkout, every uncommitted edit lands in the running board, and
conflict markers left by a merge break the CLI for every agent. Install the board from a fixed commit instead:

```bash
python3 scripts/install.py            # HEAD of main (else master, else the current HEAD)
python3 scripts/install.py --ref v1   # a commit, branch or tag; also --repo PATH, --home DIR
```

The script copies `board/agent_board` from the commit (via `git archive`, so the working tree does not matter) into
`<home>/runtime/<commit>` and atomically switches the wrapper `<home>/bin/agent-board` to it. Add `<home>/bin` to
`PATH`. The wrapper ignores the current directory and the repository state, and uses the Python interpreter the
script was run with.

Alternatives: `pip install -e .` provides the `agent-board` command, and
`PYTHONPATH=board python3 -m agent_board …` runs it from the repository root without installing.

## Updating

```bash
agent-board self-update               # the same repository and ref as the last install
agent-board self-update --ref <sha>   # another commit, or back to an earlier one
```

The running server keeps the code it was started with; restart it after an update:
`agent-board stop && agent-board serve --detach`. Old `<home>/runtime/<commit>` directories are kept (a running
server may still use one) and can be deleted by hand.

The board compares versions: after a plugin update, `serve --detach` and the skill notice that an older server is
still running and ask to restart it.

## Data

Everything lives in `$AGENT_BOARD_HOME` (default `~/.agent-board`), see [Architecture](architecture.md#data). Only
one board runs per data directory.
