# agent-board

Локальная доска задач для AI-агентов: демон на чистом Python 3.9+ (только стандартная библиотека) хранит проекты, задачи, зависимости, вопросы и переписку в файлах, отдаёт HTTP API с потоком событий (SSE) и консольную утилиту для агентов.

Данные лежат в `$AGENT_BOARD_HOME` (по умолчанию `~/.agent-board`).

## Запуск

Из корня репозитория без установки:

```bash
PYTHONPATH=board python3 -m agent_board init pay --title "Платежи"   # создать проект
PYTHONPATH=board python3 -m agent_board serve                        # запустить доску
```

Или установить пакет (`pip install -e .`) и пользоваться командой `agent-board`.

`serve` печатает ссылку вида `http://127.0.0.1:8737/?token=...`. Токен хранится в `<home>/token` и не меняется между перезапусками, порт по умолчанию — первый свободный из 8737–8746 (`--port N` задаёт его явно). На один каталог данных запускается только одна доска: повторный `serve` завершается с кодом 3.

## Команды

| Команда | Что делает |
|---|---|
| `serve [--port N]` | запустить доску |
| `init <slug> --title T` | создать проект |
| `state` | снимок проекта целиком |
| `show <key>` | одна задача |
| `status <key> <status>` | сменить статус задачи |
| `update <key> [--summary ...] [--todo-json ...] [--changed-files-json ...] [--review-json ...] [--report ...] [--branch ...] [--worktree ...] [--session-id ...]` | обновить поля задачи |
| `ask <key> --text T [--option O ...] [--context C] [--recommended O]` | задать вопрос пользователю |
| `say <thread> --text T` | написать в тред задачи или в `_project` |
| `thread <thread>` | прочитать тред |
| `add-task --json '{...}'` | добавить задачу (поля: key, title, description, done_when, repo, kind, estimate, deps) |
| `propose <add_task\|set_deps\|cancel_task> --json '{...}' [--reason R]` | предложение агента проекта |
| `events [--since N]` | журнал событий |

Все команды, кроме `serve` и `init`, обращаются к запущенной доске; если её нет — код выхода 2. Ошибки запроса — код 1.

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `AGENT_BOARD_HOME` | каталог данных (по умолчанию `~/.agent-board`) |
| `AGENT_BOARD_PROJECT` | проект по умолчанию вместо `--project` |
| `AGENT_BOARD_ACTOR` | кто действует: `agent` (по умолчанию), `user`, `project_agent` |
| `AGENT_BOARD_TASK` | задача агента; с ней `say _project` будит агента проекта (эскалация) |

## Тесты

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```
