# agent-board, план 2: исполнение — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сервер доски сам запускает агентов на разблокированные задачи в отдельных git worktree, продолжает их сессии сообщениями из карточек, ведёт агента проекта, очередь общих ресурсов и восстанавливается после прерванных ходов.

**Architecture:** Раннер (`runner.py`) подписан на события `Board` (через хаб сервера) и исполняет `agent_wake` / `agent_stop`: готовит worktree (`worktree.py`), собирает промпт (`prompts.py`) и запускает или продолжает сессию через адаптер харнесса (`harness.py`: Claude Code, Codex, тестовый fake). Каждая сессия ведёт один ход за раз; пробуждения во время хода копятся и уходят одним сообщением после него. Агенты пишут на доску только через CLI из плана 1. Общие ресурсы — `agent-board lock run <ресурс> -- <команда>` с очередью на сервере.

**Tech Stack:** Python ≥ 3.9 stdlib, `git` CLI, `subprocess`; CLI харнессов: `claude` (`-p`, `--session-id`, `--resume`, `--output-format stream-json`, `--verbose`, `--permission-mode`, `--append-system-prompt`), `codex exec --json` / `codex exec --json resume <id> <prompt>` (id сессии — из события `thread.started`, поле `thread_id`). Тесты — pytest, настоящие git-репозитории во временных папках, fake-харнесс.

**Spec:** `docs/superpowers/specs/2026-11-04-agent-board-design.md`; дорожная карта и хвосты плана 1 — `docs/superpowers/plans/2026-11-05-roadmap.md`; ядро — `README.md` и `board/agent_board/`.

## Global Constraints

- Рантайм — только стандартная библиотека Python 3.9 (`from __future__ import annotations`), внешние процессы — `git`, `claude`, `codex`.
- **Никто не пушит.** Ни раннер, ни агенты не выполняют `git push`; промпт агента это запрещает.
- **Агенты не коммитят.** Коммит в локальную ветку задачи делает раннер в момент, когда пользователь одобрил ревью (переход задачи в `approved` от `user`), сообщением `<KEY> <title>`. Это и есть код, от которого стартуют зависимые задачи в цепочке.
- Worktree задачи: `<repo>/.agent-board-worktrees/<KEY>`; `.agent-board-worktrees/` добавляется в `<repo>/.git/info/exclude`. Ветка — по шаблону `branch_template` из конфига (по умолчанию `{key}`), база — `graph.base_for(...)`.
- Харнесс и режим разрешений — из конфига проекта (`harness: {"type": "claude"|"codex"|"fake", "permission_mode": ..., "model": ...}`); по умолчанию `{"type": "claude", "permission_mode": "auto"}`.
- Параллельность: не больше `max_agents` одновременных ходов на проект и не больше `global_max_agents` (по умолчанию 6) на сервер; лишние пробуждения ждут в очереди раннера.
- Одна сессия — один ход за раз; пробуждения во время хода объединяются в одно следующее сообщение.
- Логи ходов: `<project>/logs/<KEY>.jsonl` (сырой поток харнесса), не во временных папках.
- Словарь событий — константы `EVENT_TYPES` / `WAKE_REASONS` в `board.py`; новые причины добавляются туда же.

## Review Focus

1. Сообщение пользователя приходит, пока агент ещё в ходе → оно не теряется и уходит агенту сразу после хода (Task 7).
2. Сервер перезапустили посреди хода → карточка помечена «ход прерван», сессия продолжена сообщением о прерывании, а не начата заново (Task 7, Task 8).
3. CLI харнесса упал, нет в PATH или вернул ошибку → карточка показывает ошибку хода, раннер не зацикливается на повторных запусках (Task 4, Task 7).
4. Агент, держащий общий ресурс, упал → ресурс освобождается, следующий в очереди получает его (Task 6).
5. Задача в цепочке стартует, когда зависимость одобрена → в её worktree есть коммит зависимости (Task 3, Task 8).

---

## Структура файлов

```
board/agent_board/
  worktree.py      # git: создание worktree от базы/с вливанием веток, коммит одобренного, ребейз, дифф
  harness.py       # адаптеры харнессов: Claude, Codex, Fake; запуск/продолжение хода, разбор потока, команда «открыть в терминале»
  prompts.py       # сборка промптов агента задачи и агента проекта
  prompts/task.md, prompts/project.md   # шаблоны промптов (англ.; язык общения — из конфига)
  resources.py     # очередь эксклюзивных ресурсов
  runner.py        # раннер: подписка на события, очередь ходов, лимиты, жизненный цикл агентов
  fake_agent.py    # тестовый «агент»: выполняет сценарий из промпта через CLI доски
board/tests/
  test_worktree.py, test_harness.py, test_prompts.py, test_resources.py, test_runner.py, test_e2e.py
```

---

### Task 1: Хвосты плана 1

**Files:** Modify `board/agent_board/server.py`, `docs/superpowers/plans/2026-11-05-plan-1-core.md`; Test `board/tests/test_server.py`

- [ ] **Step 1: Падающие тесты:** (a) порт по умолчанию, на котором слушает чужой сокет на `0.0.0.0`, пропускается (`_bind` берёт следующий); (b) сбой после bind в `start()` (подменить запись `server.json` исключением) освобождает блокировку и закрывает сокет — повторный `start()` успешен; (c) существующий `<home>/token` с правами 0644 после старта имеет 0600.
- [ ] **Step 2–4:** FAIL → реализация (в цикле `DEFAULT_PORTS` пропускать порт, если `_port_open(port)`; всё после bind в `start()` — в `try` с откатом; `os.chmod(token, 0o600)` при чтении) → PASS.
- [ ] **Step 5:** В плане 1 заменить упоминания `update_task(key, actor, **fields)` на `update_task(key, actor, fields: dict)`.
- [ ] **Step 6: Commit** `exec: хвосты ядра — порт, откат старта, права токена`

---

### Task 2: Модель исполнения и новые реакции Board

**Files:** Modify `model.py`, `config.py`, `board.py`; Test `test_model.py`, `test_board.py`

**Interfaces — Produces:**
- `Task` новые поля: `agent_state: str = "idle"` (`idle` / `running` / `waiting_turn` / `error`), `turn_error: str = ""`, `last_turn_at: str = ""`, `harness: str = ""`; все — в белом списке `update_task`.
- `ProjectConfig` новые поля: `harness: dict = {"type": "claude", "permission_mode": "auto"}`, `branch_template: str = "{key}"`, `global_max_agents: int = 6`, `project_brief: str = ""` (бриф агенту проекта: цель, ссылки на план и документы), `project_session_id: str | None = None`.
- `WAKE_REASONS` += `rebase`, `interrupted`; `EVENT_TYPES` += `turn_started`, `turn_finished`, `turn_failed`, `task_committed`, `lock_waiting`, `lock_acquired`, `lock_released`.
- Реакция `rebase`: когда задача с цепочкой зависимых снова переходит в `approved` (после возврата) или в `done`, каждой зависимой, у которой уже есть `session_id` и статус не `done`/`sent`/`cancelled`, — `agent_wake {key, reason: "rebase", from: <ключ зависимости>}`.
- `Board.record(type: str, data: dict) -> Event` — запись события раннером (только типы из `EVENT_TYPES`, иначе ValueError).

- [ ] **Step 1: Падающие тесты:** roundtrip новых полей; дефолты конфига; `update_task` принимает `agent_state`; повторное `approved` зависимости будит зависимую с сессией причиной `rebase`; без сессии — не будит `rebase` (её будит обычная разблокировка); `record` с неизвестным типом → ValueError.
- [ ] **Step 2–4:** FAIL → реализация → PASS (полный прогон).
- [ ] **Step 5: Commit** `exec: модель исполнения и реакция rebase`

---

### Task 3: Worktree и git

**Files:** Create `board/agent_board/worktree.py`; Test `board/tests/test_worktree.py`

**Interfaces — Produces** (все функции бросают `GitError(RuntimeError)` с выводом git при сбое; `run_git(repo: Path, *args) -> str`):
- `worktree_path(repo: Path, key: str) -> Path` — `<repo>/.agent-board-worktrees/<KEY>`.
- `ensure_worktree(repo: Path, key: str, branch: str, base: str, merge: list[str]) -> Path` — идемпотентно: если worktree есть — вернуть путь; иначе добавить `.agent-board-worktrees/` в `.git/info/exclude` (один раз), `git worktree add -b <branch> <path> <base>`, затем `git merge --no-edit <b>` для каждой ветки из `merge` (конфликт → `GitError`, worktree остаётся для разбора агентом).
- `commit_all(path: Path, message: str) -> str | None` — `git add -A` + `git commit -m`; вернуть короткий sha или `None`, если нечего коммитить.
- `rebase_onto(path: Path, new_base: str) -> bool` — `git rebase <new_base>`; при конфликте `git rebase --abort` и `False`.
- `changed_files(path: Path, base: str) -> list[str]` — изменённые относительно `base` (включая незакоммиченные и неотслеживаемые).
- `diff(path: Path, base: str) -> str` — `git diff <base>` плюс неотслеживаемые как добавленные.

- [ ] **Step 1: Падающие тесты** на настоящем репозитории в `tmp_path` (фикстура `git_repo`: `git init -b main`, коммит, `user.name/email` локально): создание от `main`; повторный вызов идемпотентен; exclude дописан один раз; вливание двух веток; конфликт вливания → `GitError`; `commit_all` создаёт коммит и возвращает `None` при чистом дереве; `rebase_onto` успешный и с конфликтом (дерево восстановлено); `changed_files` видит изменённый, новый и неотслеживаемый файлы.
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** `exec: worktree и git`

---

### Task 4: Адаптеры харнессов

**Files:** Create `harness.py`, `fake_agent.py`; Test `test_harness.py`

**Interfaces — Produces:**
- `@dataclass TurnResult(session_id: str | None, ok: bool, error: str = "", exit_code: int = 0)`.
- `class Harness` (протокол): `start_turn(cwd: Path, prompt: str, session_id: str | None, log: Path, env: dict) -> subprocess.Popen` (новая сессия, если `session_id` нет у харнесса заранее — Codex), `resume_turn(cwd, session_id, message, log, env) -> Popen`, `finish(proc: Popen, log: Path) -> TurnResult` (дожидается процесса, пишет поток в лог построчно, извлекает id сессии и ошибку), `terminal_command(cwd: Path, session_id: str) -> str`, `new_session_id() -> str | None` (Claude — uuid4, Codex — None).
- `ClaudeHarness(permission_mode, model=None)`: старт `claude -p --session-id <id> --output-format stream-json --verbose --permission-mode <m> [--model M] --append-system-prompt <system> <prompt>`; продолжение `claude -p --resume <id> --output-format stream-json --verbose --permission-mode <m> <message>`; ошибка — последнее событие `type: "result"` с `is_error: true` или ненулевой код; терминал — `cd <cwd> && claude --resume <id>`.
- `CodexHarness(sandbox="workspace-write", model=None)`: старт `codex exec --json -C <cwd> --sandbox <s> <prompt>`, id — из `{"type":"thread.started","thread_id":...}`; продолжение `codex exec --json -C <cwd> resume <id> <message>`; терминал — `cd <cwd> && codex resume <id>`.
- `FakeHarness()`: запускает `python3 -m agent_board.fake_agent`; сценарий — JSON-список шагов в промпте/сообщении после маркера `FAKE-SCRIPT:`; шаги — команды CLI доски (`status`, `ask`, `say`, `update`, `lock-run`, `write-file`, `sleep`, `fail`); id сессии — переданный или uuid4.
- `make_harness(config: dict) -> Harness` по `config["type"]`; неизвестный тип → ValueError. Отсутствующий бинарь → `TurnResult(ok=False, error="… не найден в PATH")` без исключения.
- Окружение хода: `AGENT_BOARD_HOME`, `AGENT_BOARD_PROJECT`, `AGENT_BOARD_ACTOR` (`agent` / `project_agent`), `AGENT_BOARD_TASK`, `PATH` с каталогом, где доступна команда `agent-board` (раннер передаёт `python3 -m agent_board` через обёртку-скрипт в `<project>/bin/agent-board`).

- [ ] **Step 1: Падающие тесты:** сборка командных строк Claude/Codex (без запуска, через метод `build_*_args`); разбор потоков из фикстур `tests/fixtures/claude_stream.jsonl`, `claude_error.jsonl`, `codex_stream.jsonl` (id сессии, ok/ошибка); fake: сценарий из двух шагов против живого сервера из фикстуры меняет статус задачи; отсутствующий бинарь → `ok=False` с понятной ошибкой.
- [ ] **Step 2–4:** FAIL → реализация → PASS. Фикстуры потоков — минимальные реалистичные примеры (несколько строк); в отчёте указать, откуда взят формат.
- [ ] **Step 5: Commit** `exec: адаптеры харнессов`

---

### Task 5: Промпты

**Files:** Create `prompts.py`, `prompts/task.md`, `prompts/project.md`; Test `test_prompts.py`

**Interfaces — Produces:**
- `task_prompt(task: Task, config: ProjectConfig, worktree: Path, base: str, board_cli: str) -> str` и `task_system(config) -> str`.
- `project_prompt(config, snapshot: dict, board_cli: str) -> str`.
- `wake_message(reasons: list[dict], digest: str) -> str` — одно сообщение из накопленных пробуждений: сообщения пользователя дословно (с id), ответы на вопросы, возврат («задачу вернули из <статус>»), ребейз («ветка зависимости <K> изменилась, перебазируйся на <база>»), прерывание («предыдущий ход прерван перезапуском сервера, проверь состояние и продолжи»), эскалация (для агента проекта — от какой задачи).
- `digest(events: list[Event], limit: int = 40) -> str` — краткая сводка для агента проекта: смены статусов, новые вопросы/ответы, применённые предложения, ошибки ходов.

Содержание `task.md` (обязательные пункты, текст на английском, отвечать пользователю — на `config.language`): задача, критерий готовности, worktree и правило «работать только в нём»; запрет `git commit` / `git push` и правок вне worktree; как вести карточку (`status in_progress` в начале, `update --summary/--todo-json/--changed-files-json` по ходу, `status in_review` по готовности); вопросы — `ask` с вариантами, `--context` для человека без знания предметки и `--recommended`, после вопроса продолжать независимую работу или закончить ход; общесистемные вопросы — `say _project` (эскалация); общие ресурсы — только через `lock-run`; правила проекта `config.agent_rules` дословно. `project.md`: роль, бриф `config.project_brief`, как читать доску (`state`, `show`, `thread`, `events --since`), отвечать в `_project`, писать агентам задач `say <KEY>`, менять граф только через `propose`.

- [ ] **Step 1: Падающие тесты:** промпт содержит ключ, критерий, путь worktree, запрет push/commit, `agent_rules` дословно, команды CLI; `wake_message` объединяет 3 причины в одном тексте в порядке поступления и цитирует сообщение пользователя дословно; `digest` ограничен `limit` и упоминает ошибку хода.
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** `exec: промпты агентов`

---

### Task 6: Очередь общих ресурсов

**Files:** Create `resources.py`; Modify `server.py`, `cli.py`; Test `test_resources.py`, `test_cli.py`

**Interfaces — Produces:**
- `class ResourceQueue` (в сервере, один на проект): `acquire(resource, task, pid) -> int` (позиция; 0 — получен), `poll(resource, task, pid) -> int`, `release(resource, task, pid)`, `reap()` — снимает держателей и ожидающих с мёртвым pid (`os.kill(pid, 0)`). Ресурс должен быть объявлен в `config.resources` (`{"name", "description"}`), иначе ValueError.
- API: `POST /api/p/<slug>/locks/<resource>/acquire|poll|release` `{task, pid}` → `{"position": n}`. События через `Board.record`: `lock_waiting {key, resource, position}`, `lock_acquired`, `lock_released`. Пока задача ждёт — её статус `queued` (actor `system`), после получения — обратно в прежний статус.
- CLI: `agent-board lock-run <resource> -- <cmd...>` — acquire, опрос раз в 2 с с выводом позиции в stderr, запуск команды, release в `finally`; код возврата — код команды.

- [ ] **Step 1: Падающие тесты:** два держателя — второй получает позицию 1, после release — 0; `reap` освобождает ресурс мёртвого pid и отдаёт следующему; необъявленный ресурс → 400; `lock-run` выполняет команду и освобождает ресурс даже при её ненулевом коде; статус задачи `queued` во время ожидания и возврат после.
- [ ] **Step 2–4:** FAIL → реализация → PASS.
- [ ] **Step 5: Commit** `exec: очередь общих ресурсов`

---

### Task 7: Раннер

**Files:** Create `runner.py`; Modify `server.py` (хаб: несколько подписчиков `on_event`; запуск раннера в `BoardServer` при `runner=True`); Test `test_runner.py`

**Interfaces — Produces:** `class Runner(server_url: str, token: str, home: Path, clock=None)` — работает в собственном потоке, получает события через подписку хаба (не через HTTP), действия на доске — через `Board` проекта (тот же процесс). Методы: `start()`, `stop()`, `on_event(slug, event)`.

Поведение:
- `agent_wake` для задачи без `session_id`: если лимиты позволяют — `base_for` → `ensure_worktree` → `update_task(worktree, branch, base_branch, harness, session_id=new_session_id())` → `start_turn(task_prompt)`; иначе — в очередь раннера.
- `agent_wake` для задачи с сессией: если ход идёт — добавить причину в `pending[key]`; иначе `resume_turn(wake_message(pending + эта))`.
- `rebase`: перед ходом `rebase_onto(новая база)`; при конфликте — не ребейзить, а сообщить агенту в `wake_message`, что ребейз не удался и его нужно сделать вручную.
- Ход: `agent_state=running`, `turn_started`; по завершении — `finish` → `agent_state=idle|error`, `turn_error`, `last_turn_at`, `session_id` (для Codex — из результата), `changed_files(...)` → `update_task`; `turn_finished`/`turn_failed`. Если `pending[key]` не пуст — сразу следующий ход. После 3 подряд неудачных ходов задачи — не перезапускать, `agent_state=error`, системное сообщение в тред.
- `status_changed` в `approved` от `user`: `commit_all(worktree, f"{key} {title}")` → `task_committed {key, sha}`.
- `agent_stop`: завершить процесс хода (SIGTERM, через 10 с SIGKILL), очистить `pending`.
- Агент проекта: при первом `agent_wake {key: "_project"}` или старте раннера для проекта в стадии `work` без `project_session_id` — сессия в `<project>/project-agent/` с `project_prompt`; дальнейшие пробуждения — `wake_message` + `digest(events since last_seen)`; `last_seen` хранится в `meta.json`.
- Прерванные ходы: при `start()` для каждой задачи с `agent_state=running` — `agent_state=idle`, системное сообщение «ход прерван перезапуском сервера», `agent_wake {reason: "interrupted"}`.
- Лимиты `max_agents` / `global_max_agents` считаются по идущим ходам; при завершении хода — запуск следующего из очереди (FIFO).
- Терминальная команда: `snapshot()` задачи отдаёт `terminal_command` (через `harness.terminal_command`) при наличии сессии.

- [ ] **Step 1: Падающие тесты** (fake-харнесс, настоящий git, сервер из фикстуры с `runner=True`): готовая задача запускается, создаётся worktree, сессия записана; сообщение пользователя во время хода уходит следующим ходом одним сообщением вместе с другими (Review Focus 1); лимит `max_agents=1` — вторая задача ждёт и стартует после первой; падающий ход → `error`, после 3 неудач нет перезапуска (Review Focus 3); отсутствующий бинарь → `turn_failed` с текстом; `approved` от пользователя создаёт коммит в ветке задачи; `agent_stop` убивает процесс хода; задача с `agent_state=running` при старте раннера получает `interrupted` (Review Focus 2); эскалация будит агента проекта и он получает дайджест.
- [ ] **Step 2–4:** FAIL → реализация → PASS. Тесты ждут событий через опрос `read_events` с таймаутом ≤ 10 с, без `sleep` вслепую.
- [ ] **Step 5: Commit** `exec: раннер`

---

### Task 8: Сквозная проверка и запуск

**Files:** Modify `cli.py` (`serve` запускает раннер; `--no-runner` для отладки), `README.md`; Create `board/tests/test_e2e.py`

- [ ] **Step 1: Сквозной тест** (fake-харнесс, настоящий git, `serve` в отдельном процессе через `subprocess` с временным `AGENT_BOARD_HOME`): проект с задачами A и B (B зависит от A, цепочки включены). Сценарий A: `status in_progress` → `write-file` → `ask` вопрос → (тест отвечает через API) → `status in_review`. Тест переводит A в `approved` от `user` → проверить коммит в ветке A, старт B, наличие файла A в worktree B (Review Focus 5). Затем убить сервер во время хода B (fake-шаг `sleep`), поднять заново → у B `interrupted`, сессия та же, ход продолжен (Review Focus 2).
- [ ] **Step 2: Запустить** `.venv/bin/python -m pytest board/tests/test_e2e.py -v` 3 раза подряд — PASS.
- [ ] **Step 3: README:** раздел «Исполнение» — как раннер запускает агентов, где worktree и логи, настройка `harness`, `resources`, `lock-run`, правило «не пушим, коммит — при одобрении».
- [ ] **Step 4: Ручная проверка с настоящим Claude (необязательная, по возможности):** временный репозиторий, одна задача «создай файл hello.txt с текстом hi», `harness: {"type": "claude", "permission_mode": "acceptEdits"}`; `agent-board serve` → задача проходит до `in_review`, в worktree есть `hello.txt`, в карточке есть резюме. Команды и результат — в отчёт; если `claude` недоступен в окружении исполнителя — честно отметить пропуск.
- [ ] **Step 5: Полный прогон** `.venv/bin/python -m pytest -q` — PASS.
- [ ] **Step 6: Commit** `exec: сквозной сценарий и запуск раннера`
