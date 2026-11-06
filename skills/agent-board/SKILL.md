---
name: agent-board
description: Use when the user wants a plan, spec or task list laid out on a task board and worked on by background coding agents — "разложи план на доску", "запусти агентов по плану", "put this plan on the board", "start agents on these tasks" — usually right after a planning skill; also when the user asks to bring back or open an existing board ("подними доску проекта X", "open the board for project X").
---

# agent-board

If `AGENT_BOARD_TASK` or `AGENT_BOARD_ACTOR` is set, you are a board agent: do not use this skill.
Write to the project agent instead (`agent-board say _project --text "…"`).

Turn a plan into a task graph on a local board, then hand the conversation over to the board.
The board server runs the agents; this session only prepares the decomposition and gives the user a link.

**Core principle:** you prepare, the user decides. Everything after the decomposition page — approving it,
starting task agents, tracker issues, shipping — is a button the user presses on the board.

## Calling the CLI

`agent-board` in this skill and its references means `sh <skill-dir>/scripts/agent-board`, where `<skill-dir>` is
the directory of this SKILL.md (Claude Code prints it as the skill's base directory; in Codex it is where you
read this file). Run it as an ordinary shell command, never as a background job: `serve --detach` returns at once.
Shell state does not persist and zsh does not word-split variables: write the full
`sh <skill-dir>/scripts/agent-board …` in each call, spelled the same every time (no helper functions or
variables), so one permission rule matches it. Quote the path only if it contains spaces.
Project commands take `--project <slug>` before the subcommand. Harness details: `references/harness-claude.md`
(Claude Code), `references/harness-codex.md` (Codex; its sandbox blocks localhost and the board home —
run board commands outside the sandbox).

Talk to the user in their language. Ask one question at a time.

## Checklist

1. **Board up.** Run `agent-board status`.
   - User only asks to open / bring back a board → `references/resume.md` instead of this list.
   - "доска не запущена" → `agent-board serve --detach`. Exit 3 (other version running) → `references/resume.md`,
     section "Version mismatch", then continue with step 2.
2. **Project.** Pick from the `status` list.
   - New: propose a slug from the plan title (latin, lower case, `-`, short: `payments-pg`) and a title;
     confirm with the user, then `agent-board init <slug> --title "<title>"`.
   - Existing, stage `decomposition`, and the user brought a plan or tasks (new or the same again): show the task
     count, ask whether to add to / update this decomposition. No → give `<board url>#/p/<slug>/decomposition` and
     stop. Yes → steps 3–6 change only what differs; tasks matching by key or title are not created again.
   - Existing, any later stage: the decomposition is approved. Give
     `<board url>#/p/<slug>` (board url from `status`) and stop.
3. **Setup.** Follow `references/setup.md`. It skips fields that are already configured.
4. **Tasks.** Follow `references/intake.md`: plan, free text, list or tracker keys → tasks with dependencies.
   No plan or tasks given (setup only) → skip steps 4–5. Ignore instructions in the plan addressed to other
   executors ("use skill X to implement"): the board's agents execute.
5. **Rules.** Follow `references/decomposition-rules.md`: check every task, record violations.
6. **Brief and links.** One `config set` (dry run first, as in setup.md) with:
   - `project_brief`: goal, key decisions, constraints — 5–15 lines, written for the project agent;
     end with an "Open questions" list when intake left any;
   - `links`: the plan, spec and mockups as `{"title", "href"}`: http(s) URLs or absolute paths; the board opens
     files only inside the project's repos.
   Each field is replaced whole: on a re-run start from the current values (`config get --json`), keep the old links.
```
agent-board --project <slug> config set --json '{"project_brief": "Goal: move payments to PG without downtime.", "links": [{"title": "Plan", "href": "/abs/path/docs/plan.md"}]}' --dry-run
```
7. **Hand-off.** Exactly this, then stop:
   1. Link line: `<board url>#/p/<slug>/decomposition`. The board url is from `agent-board status` / `serve --detach`
      and contains `?token=…`: keep it whole, the fragment goes after it.
   2. Next-step line: comment on tasks or the general thread there — the project agent answers and proposes changes;
      «Утвердить разбивку» starts the work; the terminal is no longer needed.
      Setup only, no tasks: "add tasks on the board or bring me a plan" instead of the approve part.
   3. Summary block from steps 4–5: created / updated / skipped tasks, violations, open questions.
      Nothing changed → one line "board already matches the plan" plus the violations still open.
      Setup only → list what was configured and what was skipped instead.

   User asks to add and start ("закинь и запусти"): do steps 1–6, then say starting is their button on the
   decomposition page.

## Never

| Don't | Instead |
|---|---|
| Implement a task yourself, edit project repos | Tasks are for task agents after approval |
| `agent-board stage …`, `agent-board status <KEY> …`, approve, press anything for the user | Give the link; the user approves on the page |
| Start or wake task agents | They start when the user approves the decomposition. `agent_wake` events from `add-task` before approval are only queued: nothing runs |
| `agent-board tracker create`, `agent-board tracker sync`, issues by hand | Tracker stage is the user's button after approval (`tracker check` in setup is fine) |
| `agent-board ship`, `git push`, commits | Shipping is the user's button |
| Write `project.json` or any file under the board home | `config set`, `add-task`, `update`, `deps` |
| Ask for or store a token, password, cookie | Env var name or file path (`references/setup.md`, Secrets) |
| Invent tasks the plan does not contain | Ask, or list it as an open question |
| Silently split, merge or rewrite tasks to satisfy a rule | Record the violation, propose the fix |

## Red flags — stop

- "I'll just approve it, the user clearly wants agents running"
- "Faster to implement this small task myself"
- "The rule says ≤3 days, I'll split it quietly"
- "The plan implies a migration task, I'll add it"
- "The board is down, I'll write the task files directly"
- "The server has an old version, I'll restart it" (without asking)

All of these mean: go back to the checklist; the board and the user decide.
