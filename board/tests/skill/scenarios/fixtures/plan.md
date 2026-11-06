# Items CLI Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development to implement this plan task by task.

**Goal:** Turn the tiny `app.py` list helper into a small items CLI with persistent storage, export research and docs.

**Architecture:** `storage.py` keeps items in a JSON file; `cli.py` exposes commands on top of it; logging is configured once at start.

**Tech Stack:** Python 3.9, pytest, stdlib only.

---

### Task 1: Add storage module

**Files:** Create `storage.py`, `tests/test_storage.py`

- [ ] Write `load_items(path)` and `save_items(path, items)` with a JSON file.
- [ ] Tests: round trip, missing file returns an empty list.

**Estimate:** 1 day. **Done when:** `pytest tests/test_storage.py` passes.

### Task 2: Add CLI commands on top of storage

**Depends on:** Task 1

**Files:** Create `cli.py`, `tests/test_cli.py`

- [ ] Commands `add`, `list`, `remove` that read and write through `storage.py`.
- [ ] Tests for every command with a temporary storage file.

**Estimate:** 3 days. **Done when:** `pytest tests/test_cli.py` passes and `python cli.py list` prints stored items.

### Task 3: Add help output and an end-to-end test

**Depends on:** Task 2

**Files:** Modify `cli.py`; create `tests/test_e2e.py`

- [ ] Usage text for `--help` and for unknown commands.
- [ ] End-to-end test: add two items, list them, remove one.

**Estimate:** 1 day. **Done when:** `pytest tests/test_e2e.py` passes.

### Task 4: Add logging configuration

**Files:** Create `logging_setup.py`

- [ ] One function `setup_logging(level)`; the CLI calls it at start.
- [ ] Test that the level from an environment variable is applied.

**Estimate:** 1 day. **Done when:** `pytest tests/test_logging.py` passes.

### Task 5: Update README usage section

**Files:** Modify `README.md`

- [ ] Document the three commands with one example each.

**Estimate:** 0.5 day. **Done when:** README shows `add`, `list` and `remove` examples that match the real output.

### Task 6: Research export formats

**Type:** research (the result is a report, no code changes)

- [ ] Compare CSV and JSON export for items: size, readability, tooling.
- [ ] Recommend one format with reasons.

**Estimate:** 1 day. **Done when:** a report in `docs/export-research.md` recommends one format with at least two reasons.
