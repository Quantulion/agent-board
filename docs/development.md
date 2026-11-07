# Development

The board is Python 3.9+ with the standard library only; the UI is ES modules in `board/agent_board/static/` with no
build step and no dependencies. Do not add runtime dependencies.

## Python tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The full run takes several minutes. Slow tests (skill scenarios, the wrapper, installation, end-to-end, concurrency
and runner tests with the fake agent) carry the `slow` marker. Tests never touch your real `~/.agent-board`: the test
setup refuses to run against it.

`scripts/affected-tests.py` prints the test files touched by the current changes: uncommitted edits and new files
against `HEAD`, or everything that differs from a branch with `--base <branch>`. Shared fixtures, `pyproject.toml` or
a file without a rule select the whole suite; with no changes it prints nothing, and pytest then runs everything.

- While working: the affected tests, slow ones included —
  `.venv/bin/python -m pytest -q $(python3 scripts/affected-tests.py)` — and the fast suite for everything else —
  `.venv/bin/python -m pytest -q -m "not slow"`.
- Before review: the full suite, `.venv/bin/python -m pytest -q` (or the parallel run below).

When a new package module appears, add it to `RULES` in `scripts/affected-tests.py` (a test checks that every module
has a rule).

`scripts/test-parallel.py` runs the suite (or the given paths) in several pytest processes:
`.venv/bin/python scripts/test-parallel.py`. It collects the tests once and splits them into groups of similar
duration (from the previous run, cached in `.pytest_cache`); `test_runner.py` and the skill tests are split by test,
other files go whole. Tests marked `default_port` use the board's default port and run one at a time after the
other groups. `-n` sets the number of processes (4 by default), `--plan` only shows the groups, pytest options go
after `--` (for example `-- -m "not slow"`). The exit code is non-zero if a test failed or fewer tests ran than were
collected.

## UI tests

UI logic is tested with Node's built-in test runner:

```bash
node --test 'board/tests/js/*.test.mjs'
```

Headless Chrome checks live in `ui-tests/` (puppeteer is installed only there; Node is needed only for development).
Each script starts a real board on a temporary directory with the fake agent and saves screenshots to
`ui-tests/out/`:

```bash
cd ui-tests && npm ci && npm test     # all scripts, stops at the first failure
npm run <name>                         # one script, or node ui-tests/<name>.mjs from the repository root
```

The Python interpreter for the board is the first of: `AGENT_BOARD_PYTHON`, `.venv` in the repository root, `.venv`
of the main checkout (so the checks run from a worktree), `python3` from `PATH`.

| Script | Checks |
|---|---|
| `skeleton` | skeleton, live updates, safe markdown |
| `board` | 14 tasks in all statuses, status menu, toasts |
| `card` | task card with the runner and the fake agent: answering a question, thread, diff, resizing |
| `graph` | graph, the "Project" panel, a new task |
| `decomposition` | the decomposition page |
| `smoke` | end-to-end pre-release scenario, reconnecting after a restart, load (60 tasks, 200 events in 5 s; thresholds `SMOKE_LONGTASK_MS`, `SMOKE_SEND_LIMIT_MS`) |
| `integrations` | creating issues in a fake Jira, two review rounds with a fake crit, bulk shipping, tracker status sync |
| `english` | the English interface has no Cyrillic on the main screens |
| `settings` | the settings window: saving, server-side errors next to the field, the "Review off" mark |
| `slot` | waiting for a free slot: which limit is reached and where to raise it |
| `retry` | a turn error and the "Retry" button in the card, the "Project" panel and "Waiting for you" |
| `thread-agent` | the "agent is working" indicator in task and project threads |
| `proposals` | project agent proposals: what changes, applying, rejecting, stale proposals |
| `interrupt` | the "Interrupt current work" checkbox in task and project threads |

Some scripts also take snapshots of design mockups for comparison when those files are present locally; they are not
part of the repository.

### README demo

`docs/media/demo.gif` is recorded from a demo project: the board, a task card with an agent question, its thread
and diff, and the project agent proposing a new task. Nothing real runs: the only agent turn is the fake agent, the
repositories are temporary and have no remote, and no ship command is set. Re-record it after visible UI changes
(needs Pillow: `python3 -m pip install pillow`):

```bash
cd ui-tests && npm run readme-media
```

`readme-media.mjs` saves the frames to `ui-tests/out/readme/frames`, and `frames_to_gif.py` turns them into a GIF
with a steady frame rate and one palette for the whole clip.

## Versions

Every release bumps the version in `pyproject.toml`, `board/agent_board/__init__.py`, `.claude-plugin/plugin.json`
and `.claude-plugin/marketplace.json` (tests check that they match). By the version the board notices that an old
server is still running after an update and asks to restart it.

## Publishing

`scripts/export-repo.py` exports the current tree (tracked and new non-ignored files) into a new repository as a
single commit. The author comes only from the arguments, the machine's git settings are not used, and nothing is
pushed:

```bash
.venv/bin/python -m pytest -q board/tests/test_publishability.py   # no internal names or personal paths
python3 scripts/export-repo.py ../agent-board-public \
    --author-name "Your Name" --author-email "<id>+<user>@users.noreply.github.com"
```

Use GitHub's noreply address for the commit email (Settings → Emails → "Keep my email addresses private"). The
repository URL in `.claude-plugin/plugin.json` and `marketplace.json` (`homepage`, `repository`) must match the
repository created on GitHub.
