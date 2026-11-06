# Bring back a board

The user asks to bring back or open the board of project X ("подними доску проекта X"). The board keeps all
state on disk: starting the server restores it, agents included. Nothing needs reconnecting.

## Steps

1. `agent-board status` → is the board running, and which projects exist (slug, title, stage, task count).
   Match X to a slug or title. Not found → show the list and ask which one. Do not `init` a new project here.
2. Not running → `agent-board serve --detach`. It prints the board URL. Running → `status` printed the URL.
3. Give the user the right link:
   - stage `decomposition`: `<url>#/p/<slug>/decomposition` (the decomposition is not approved yet);
   - other stages: `<url>#/p/<slug>`.
4. Short state summary from `agent-board --project <slug> state`, in the user's language, at most ~8 lines:
   stage; task counts by `status`; then what waits for the user:
   - tasks with `open_questions` > 0;
   - open `proposals` of the project agent;
   - tasks `in_review`, tasks `approved` not yet shipped;
   - tasks with `agent_state` `error`, `agent_parked`, or an `external_blocker`;
   - `project_agent.state` `error`, unread project thread (`project_unread`);
   - at stage `decomposition`: tasks with `rule_violations`.
   Answer and act on nothing yourself: point to the link.

## Version mismatch (exit 3)

`serve --detach` exits 3: a board of another version is running (after an update of the skill).
Restarting interrupts every running agent turn in every project; sessions survive and resume later.
Tell the user that, ask, and only on yes:
```
agent-board stop
agent-board serve --detach
```
On no: keep using the running board (give its URL from `status`) and mention it is an older version.

## Troubleshooting

| Symptom | Cause | Do |
|---|---|---|
| `python3 not found` / `Python 3.9+ is required`, exit 127 | No suitable Python | Ask the user to install Python 3.9+ or set `AGENT_BOARD_PYTHON` to one |
| `serve --detach` fails, port in the log | Ports 8737–8746 busy | Show the log tail it printed; ask what holds the ports, or start with `--port N` |
| Exit 2 "доска не запущена" right after a successful start | Codex sandbox blocks localhost | Run board commands outside the sandbox |
| `stop`: pid "не похож на сервер доски" | Stale `server.json` | Do not kill processes; show the message to the user |
| Project listed with "не удалось прочитать проект" | Damaged project files | Do not edit files; show the path, ask the user |
